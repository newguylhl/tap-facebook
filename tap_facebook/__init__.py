#!/usr/bin/env python3
import json
import os
import os.path
import sys
import time

import datetime
from datetime import timezone
import dateutil

import attr
import pendulum
import requests
import backoff

import singer
import singer.metrics as metrics
from singer import utils, metadata
from singer import SingerConfigurationError, SingerDiscoveryError, SingerSyncError
from singer import (transform,
                    UNIX_MILLISECONDS_INTEGER_DATETIME_PARSING,
                    Transformer, _transform_datetime)
from singer.catalog import Catalog, CatalogEntry

from functools import partial

from facebook_business import FacebookAdsApi
import facebook_business.adobjects.adaccount as fb_account
import facebook_business.adobjects.adcreative as fb_creative
import facebook_business.adobjects.ad as fb_ad
import facebook_business.adobjects.adset as fb_ad_set
import facebook_business.adobjects.campaign as fb_campaign
import facebook_business.adobjects.adsinsights as adsinsights
import facebook_business.adobjects.user as fb_user

from facebook_business.exceptions import FacebookError, FacebookRequestError, FacebookBadObjectError

TODAY = pendulum.today()

API = None
FB_USER = None

INSIGHTS_MAX_WAIT_TO_START_SECONDS = 2 * 60
INSIGHTS_MAX_WAIT_TO_FINISH_SECONDS = 30 * 60
INSIGHTS_MAX_ASYNC_SLEEP_SECONDS = 5 * 60

INSIGHTS_BATCH_SIZE = 7

RECORD_COUNT = 0
USEFUL_RECORD_COUNT = 0

RESULT_RETURN_LIMIT = 100

ONLY_ACTIVE = {
    "field": "effective_status",
    "operator": "IN",
    "value": ["ACTIVE"],
}

STREAMS = [
    'adaccounts',
    'adcreative',
    'ads',
    'adsets',
    'campaigns',
    'accounts_insights',
    'ads_insights',
    "ads_insights_age_gender",
    "ads_insights_device_platform",
    "ads_insights_placement",

    # not checked and not used
    'ads_insights_age_and_gender',
    'ads_insights_country',
    'ads_insights_platform_and_device',
    'ads_insights_region',
    'ads_insights_dma',
]

REQUIRED_CONFIG_KEYS = [
    'start_date',
    'account_id',
    'user_id',
    'access_token',
]
UPDATED_TIME_KEY = 'updated_time'
START_DATE_KEY = 'date_start'

BOOKMARK_KEYS = {
    'ads': UPDATED_TIME_KEY,
    'adsets': UPDATED_TIME_KEY,
    'campaigns': UPDATED_TIME_KEY,
    'ads_insights': START_DATE_KEY,
    'ads_insights_age_gender': START_DATE_KEY,
    'ads_insights_device_platform': START_DATE_KEY,
    'ads_insights_placement': START_DATE_KEY,

    # not checked and not used
    'ads_insights_age_and_gender': START_DATE_KEY,
    'ads_insights_country': START_DATE_KEY,
    'ads_insights_platform_and_device': START_DATE_KEY,
    'ads_insights_region': START_DATE_KEY,
    'ads_insights_dma': START_DATE_KEY,
}

LOGGER = singer.get_logger()

CONFIG = {}


class TapFacebookException(Exception):
    pass


class InsightsJobTimeout(TapFacebookException):
    pass


def transform_datetime_string(dts):
    parsed_dt = dateutil.parser.parse(dts)
    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
    else:
        parsed_dt = parsed_dt.astimezone(timezone.utc)
    return singer.strftime(parsed_dt)


class MySingerSyncError(Exception):
    def __init__(self, message):
        super().__init__(message)


class MySingerConfigurationError(Exception):
    def __init__(self, message):
        super().__init__(message)


def raise_from(singer_error, fb_error, info):
    """Makes a pretty error message out of FacebookError object

    FacebookRequestError is the only class with more info than the exception string so we pull more
    info out of it

    info = {
        'timestamp': int(time.time())
        'type': schema.name
        'action': information or function (_call_get_ad_objects, do_sync, prepare_record, sync single, sync batch, ...)
        'user': user_id
        'account': ad_account_id (is empty when schema.name == 'adaccounts')
        'processing_id': single or multiple (separated by comma)
    }
    """
    if isinstance(fb_error, FacebookRequestError):
        error_message_json = {
            "type": 'FacebookRequestError',
            "info": info,
            "method": fb_error.request_context().get('method', 'Unknown HTTP Method'),
            "status": fb_error.http_status(),
            "response": fb_error.body(),
        }
        error_message = '%s %s' % (singer_error.__name__, json.dumps(error_message_json))
        raise singer_error(error_message) from fb_error
    else:
        # All other facebook errors are `FacebookError`s and we handle
        # them the same as a python error
        error_message = str(fb_error)
        raise singer_error(error_message) from fb_error


def retry_pattern(backoff_type, exception, **wait_gen_kwargs):
    def log_retry_attempt(details):
        _, exception, _ = sys.exc_info()
        LOGGER.info(exception)
        LOGGER.info('Caught retryable error after %s tries. Waiting %s more seconds then retrying...',
                    details["tries"],
                    details["wait"])

        if isinstance(exception, TypeError) and str(exception) == "string indices must be integers":
            LOGGER.info('TypeError due to bad JSON response')

    def should_retry_api_error(exception):
        if isinstance(exception, FacebookBadObjectError):
            return True
        elif isinstance(exception, FacebookRequestError):
            return exception.api_transient_error() \
                   or exception.api_error_subcode() == 99 \
                   or exception.http_status() == 500
        elif isinstance(exception, InsightsJobTimeout):
            return True
        elif isinstance(exception, TypeError) and str(exception) == "string indices must be integers":
            return True
        return False

    return backoff.on_exception(
        backoff_type,
        exception,
        jitter=None,
        on_backoff=log_retry_attempt,
        giveup=lambda exc: not should_retry_api_error(exc),
        **wait_gen_kwargs
    )


@attr.s
class Stream(object):
    name = attr.ib()
    account = attr.ib()
    stream_alias = attr.ib()
    catalog_entry = attr.ib()

    def automatic_fields(self):
        fields = set()
        if self.catalog_entry:
            props = metadata.to_map(self.catalog_entry.metadata)
            for breadcrumb, data in props.items():
                if len(breadcrumb) != 2:
                    continue  # Skip root and nested metadata

                if data.get('inclusion') == 'automatic':
                    fields.add(breadcrumb[1])
        return fields

    def fields(self):
        fields = set()
        if self.catalog_entry:
            props = metadata.to_map(self.catalog_entry.metadata)
            for breadcrumb, data in props.items():
                if len(breadcrumb) != 2:
                    continue  # Skip root and nested metadata

                if data.get('selected') or data.get('inclusion') == 'automatic':
                    fields.add(breadcrumb[1])
        return fields


def ad_object_success(response, stream=None, count=None):
    """
    A success callback for the FB Batch endpoint used when syncing ad objects. Needs the stream
    to resolve schema refs and transform the successful response object.
    """
    refs = load_shared_schema_refs()
    schema = singer.resolve_schema_references(stream.catalog_entry.schema.to_dict(), refs)

    global RECORD_COUNT
    RECORD_COUNT += count
    rec = response.json()
    record = Transformer(pre_hook=transform_date_hook).transform(rec, schema)
    singer.write_record(stream.name, record, stream.stream_alias, utils.now())


def ad_object_failure(response, info=None):
    """
    A failure callback for the FB Batch endpoint used when syncing ad objects. Raises the error
    so it fails the sync process.
    """
    raise_from(MySingerSyncError, response.error(), info)


@attr.s
class IncrementalStream(Stream):

    state = attr.ib()

    def __attrs_post_init__(self):
        self.current_bookmark = get_start(self, UPDATED_TIME_KEY)

    def __iter__(self):
        params = {'limit': RESULT_RETURN_LIMIT}
        if CONFIG.get('specified_ids'):
            specified_ids = CONFIG.get('specified_ids')
            params['filtering'] = [
                {
                    'field': 'id',
                    'operator': 'IN',
                    'value': specified_ids,
                }
            ]
            LOGGER.info('Syncing %s | user: %s | account: %s | specified ids count: %s',
                        self.name,
                        CONFIG['user_id'],
                        self.account.get_id() if self.account else '',
                        len(specified_ids))
        else:
            if CONFIG.get('only_time_range', False):
                params['filtering'] = [
                    {
                        'field': UPDATED_TIME_KEY,
                        'operator': 'IN_RANGE',
                        'value': [
                            pendulum.parse(CONFIG['start_date']).int_timestamp,
                            pendulum.parse(CONFIG['end_date']).add(days=1).int_timestamp - 1,
                        ],
                    }
                ]

            if CONFIG.get('only_active', False) \
                    and self.name in ['ads', 'adsets', 'campaigns', 'adcreative']:
                if params.__contains__('filtering'):
                    params['filtering'].append(ONLY_ACTIVE)
                else:
                    params['filtering'] = [ONLY_ACTIVE]

        LOGGER.info('Syncing %s | user: %s | account: %s | request params: %s',
                    self.name,
                    CONFIG['user_id'],
                    self.account.get_id() if self.account else '',
                    params)

        if self.name == 'ads':
            ad_objects = self.account.get_ads(fields=self.fields(), params=params)
        elif self.name == 'adsets':
            ad_objects = self.account.get_ad_sets(fields=self.fields(), params=params)
        elif self.name == 'campaigns':
            ad_objects = self.account.get_campaigns(fields=self.fields(), params=params)
        elif self.name == 'adcreative':
            # can only filter creative by ad
            ads = self.account.get_ads(fields=['creative'], params=params)
            # send requests by batch
            creative_id_set = set()
            for ad in ads:
                creative_id_set.add(ad.export_all_data()['creative']['id'])
            if creative_id_set:
                self.fetch_creative_in_batch(list(creative_id_set), params)
            ad_objects = list()
        elif self.name == 'adaccounts':
            ad_objects = FB_USER.get_ad_accounts(fields=self.fields(), params=params)
        else:
            raise TapFacebookException('Unknown stream {}'.format(self.name))

        def add_info(raw, info):
            raw.update(info)
            return raw

        global RECORD_COUNT
        for ad_object in ad_objects:
            RECORD_COUNT += 1
            if self.name == 'adaccounts':
                yield {'record': add_info(ad_object.export_all_data(),
                                          {
                                              'user_id': CONFIG['user_id'],
                                              'user_access_token': CONFIG['access_token'],
                                          })}
            else:
                yield {'record': ad_object.export_all_data()}

    def fetch_creative_in_batch(self, ad_object_id_list, params):
        # Create the initial batch
        batch_request_size = 50
        api_batch = API.new_batch()
        batch_count = 0
        total_count = len(ad_object_id_list)
        processed_count = 0
        processing_id_list = list()

        info = {
            'timestamp': int(time.time()),
            'type': self.name,
            'action': 'do_sync',
            'user': CONFIG['user_id'],
            'account': self.account.get_id() if self.account else '',
        }

        # This loop syncs minimal ad objects
        for ad_object_id in ad_object_id_list:
            # Execute and create a new batch
            if batch_count % batch_request_size == 0:
                api_batch.execute()
                api_batch = API.new_batch()
                # print progress
                percentage = "{:.0%}".format(processed_count / total_count)
                LOGGER.info('Syncing %s | user: %s | account: %s | total %s | processed %s | percentage %s ',
                            self.name,
                            CONFIG['user_id'],
                            self.account.get_id() if self.account else '',
                            total_count,
                            processed_count,
                            percentage)

            # Add a call to the batch with the full object
            info['processing_id'] = ','.join(processing_id_list)
            fb_creative.AdCreative(fbid=ad_object_id, api=API)\
                .api_get(fields=self.fields(),
                         params=params,
                         batch=api_batch,
                         success=partial(ad_object_success, stream=self, count=1),
                         failure=partial(ad_object_failure, info=info))
            batch_count = batch_count + 1
            processed_count = processed_count + 1
            processing_id_list.append(ad_object_id)

        # Ensure the final batch is executed
        api_batch.execute()
        # print progress
        processed_count = total_count
        percentage = "{:.0%}".format(processed_count / total_count)
        LOGGER.info('Syncing %s | user: %s | account: %s | total %s | processed %s | percentage %s ',
                    self.name,
                    CONFIG['user_id'],
                    self.account.get_id() if self.account else '',
                    total_count,
                    processed_count,
                    percentage)


class AdCreative(IncrementalStream):
    """
    doc: https://developers.facebook.com/docs/marketing-api/reference/adgroup/adcreatives
    """

    field_class = fb_creative.AdCreative.Field
    key_properties = ['id']
    stream_name = 'adcreative'


class AdAccounts(IncrementalStream):
    """
    doc: https://developers.facebook.com/docs/marketing-api/reference/ad-account
    """

    field_class = fb_account.AdAccount.Field
    key_properties = ['id']
    stream_name = 'adaccounts'


class Ads(IncrementalStream):
    """
    doc: https://developers.facebook.com/docs/marketing-api/reference/adgroup
    """

    field_class = fb_ad.Ad.Field
    key_properties = ['id']
    stream_name = 'ads'


class AdSets(IncrementalStream):
    """
    doc: https://developers.facebook.com/docs/marketing-api/reference/ad-campaign
    """

    field_class = fb_ad_set.AdSet.Field
    key_properties = ['id']
    stream_name = 'adsets'


class Campaigns(IncrementalStream):
    """
    doc: https://developers.facebook.com/docs/marketing-api/reference/ad-campaign-group
    """

    field_class = fb_campaign.Campaign.Field
    key_properties = ['id']
    stream_name = 'campaigns'


ALL_ACTION_ATTRIBUTION_WINDOWS = [
    '1d_click',
    '7d_click',
    '28d_click',
    '1d_view',
    '7d_view',
    '28d_view'
]

ALL_ACTION_BREAKDOWNS = [
    'action_type',
    'action_target_id',
    'action_destination'
]


def get_start(stream, bookmark_key):
    tap_stream_id = stream.name
    state = stream.state or {}
    current_bookmark = singer.get_bookmark(state, tap_stream_id, bookmark_key)
    if current_bookmark is None:
        if isinstance(stream, IncrementalStream):
            return None
        else:
            LOGGER.info("no bookmark found for %s, using start_date instead...%s", tap_stream_id, CONFIG['start_date'])
            return pendulum.parse(CONFIG['start_date'])
    LOGGER.info("found current bookmark for %s:  %s", tap_stream_id, current_bookmark)
    return pendulum.parse(current_bookmark)


def advance_bookmark(stream, bookmark_key, date):
    tap_stream_id = stream.name
    state = stream.state or {}
    LOGGER.info('advance(%s, %s)', tap_stream_id, date)
    date = pendulum.parse(date) if date else None
    current_bookmark = get_start(stream, bookmark_key)

    if date is None:
        LOGGER.info('Did not get a date for stream %s not advancing bookmark', tap_stream_id)
    elif not current_bookmark or date > current_bookmark:
        LOGGER.info('Bookmark for stream %s is currently %s, advancing to %s', tap_stream_id, current_bookmark, date)
        state = singer.write_bookmark(state, tap_stream_id, bookmark_key, str(date))
    else:
        LOGGER.info('Bookmark for stream %s is currently %s not changing to %s', tap_stream_id, current_bookmark, date)
    return state


@attr.s
class AdsInsights(Stream):
    field_class = adsinsights.AdsInsights.Field
    base_properties = {
        'ad': ['campaign_id', 'adset_id', 'ad_id', 'date_start'],
        'account': ['account_id', 'date_start', 'date_stop'],
    }

    state = attr.ib()
    options = attr.ib()
    action_breakdowns = attr.ib(default=ALL_ACTION_BREAKDOWNS)
    level = attr.ib(default='ad')
    action_attribution_windows = attr.ib(
        default=ALL_ACTION_ATTRIBUTION_WINDOWS)
    time_increment = attr.ib(default=1)

    bookmark_key = START_DATE_KEY

    invalid_insights_fields = ['impression_device', 'publisher_platform', 'platform_position',
                               'age', 'gender', 'country', 'placement', 'region', 'dma', 'device_platform']

    # pylint: disable=no-member,unsubscriptable-object,attribute-defined-outside-init
    def __attrs_post_init__(self):
        self.breakdowns = self.options.get('breakdowns') or []
        self.key_properties = self.base_properties[self.level][:]
        if self.options.get('primary-keys'):
            self.key_properties.extend(self.options['primary-keys'])

    def job_params(self):
        start_date = get_start(self, self.bookmark_key)

        # default: 28
        buffer_days = CONFIG.get('insights_buffer_days', 28)

        buffered_start_date = start_date.subtract(days=buffer_days)

        end_date = pendulum.now()
        if CONFIG.get('end_date'):
            end_date = pendulum.parse(CONFIG.get('end_date'))

        global RESULT_RETURN_LIMIT
        global INSIGHTS_BATCH_SIZE
        # Some automatic fields (primary-keys) cannot be used as 'fields' query params.
        while buffered_start_date <= end_date:
            loop_days = (end_date - buffered_start_date).days
            if loop_days < INSIGHTS_BATCH_SIZE:
                loops = loop_days + 1
            else:
                loops = INSIGHTS_BATCH_SIZE
            # Pull latest data first
            time_ranges = list()
            for i in range(loops):
                time_ranges.append(
                    {
                        'since': end_date.subtract(days=i).to_date_string(),
                        'until': end_date.subtract(days=i).to_date_string(),
                    }
                )
            yield {
                'level': self.level,
                'action_breakdowns': list(self.action_breakdowns),
                'breakdowns': list(self.breakdowns),
                'limit': RESULT_RETURN_LIMIT,
                'fields': list(self.fields().difference(self.invalid_insights_fields)),
                'time_increment': self.time_increment,
                'action_attribution_windows': list(self.action_attribution_windows),
                'time_ranges': time_ranges,
            }
            if loop_days < INSIGHTS_BATCH_SIZE:
                break
            else:
                end_date = end_date.subtract(days=INSIGHTS_BATCH_SIZE)

    @retry_pattern(backoff.expo, (FacebookRequestError, InsightsJobTimeout, FacebookBadObjectError, TypeError), max_tries=5, factor=5)
    def run_job(self, params):
        LOGGER.info('Syncing %s | user: %s | account: %s | params: %s',
                    self.name,
                    CONFIG['user_id'],
                    self.account.get_id(),
                    params)
        job = self.account.get_insights(  # pylint: disable=no-member
            params=params,
            is_async=True)
        status = None
        time_start = time.time()
        sleep_time = 10
        while status != "Job Completed":
            duration = time.time() - time_start
            job = job.api_get()
            status = job['async_status']
            percent_complete = job['async_percent_completion']

            job_id = job['id']
            LOGGER.info('Syncing %s | user: %s | account: %s | status: %s | percentage: %d%%',
                        self.name,
                        CONFIG['user_id'],
                        self.account.get_id(),
                        status,
                        percent_complete)

            if status == "Job Completed":
                return job

            if duration > INSIGHTS_MAX_WAIT_TO_START_SECONDS and percent_complete == 0:
                pretty_error_message = ('Insights job {} did not start after {} seconds. '
                                        'This is an intermittent error and may resolve itself '
                                        'on subsequent queries to the Facebook API. '
                                        'You should deselect fields from the schema that are not necessary, '
                                        'as that may help improve the reliability of the Facebook API.')
                raise InsightsJobTimeout(pretty_error_message.format(job_id, INSIGHTS_MAX_WAIT_TO_START_SECONDS))
            elif duration > INSIGHTS_MAX_WAIT_TO_FINISH_SECONDS and status != "Job Completed":
                pretty_error_message = ('Insights job {} did not complete after {} seconds. '
                                        'This is an intermittent error and may resolve itself '
                                        'on subsequent queries to the Facebook API. '
                                        'You should deselect fields from the schema that are not necessary, '
                                        'as that may help improve the reliability of the Facebook API.')
                raise InsightsJobTimeout(pretty_error_message.format(job_id,
                                                                     INSIGHTS_MAX_WAIT_TO_FINISH_SECONDS//60))

            LOGGER.info('Syncing %s | user: %s | account: %s | sleeping for %d seconds until the job is done',
                        self.name,
                        CONFIG['user_id'],
                        self.account.get_id(),
                        sleep_time)
            time.sleep(sleep_time)
            if sleep_time < INSIGHTS_MAX_ASYNC_SLEEP_SECONDS:
                sleep_time = 2 * sleep_time
        return job

    def __iter__(self):
        job_tag = {
            'type': self.name,
            'user': CONFIG['user_id'],
            'account': self.account.get_id(),
        }
        for params in self.job_params():
            with metrics.job_timer(job_tag):
                job = self.run_job(params)

            global RECORD_COUNT
            global USEFUL_RECORD_COUNT
            min_date_start_for_job = None
            count = 0
            useful_count = 0
            for obj in job.get_result():
                RECORD_COUNT += 1
                count += 1
                rec = obj.export_all_data()
                if not min_date_start_for_job or rec['date_stop'] < min_date_start_for_job:
                    min_date_start_for_job = rec['date_stop']
                # skip loading useless insights records
                if int(rec['impressions']) == 0 and int(rec['spend']) == 0:
                    continue
                else:
                    USEFUL_RECORD_COUNT += 1
                    useful_count += 1
                    yield {'record': rec}
            LOGGER.info('Syncing %s | user: %s | account: %s | got %d results (%s useful) for the job with params %s',
                        self.name,
                        CONFIG['user_id'],
                        self.account.get_id(),
                        count,
                        useful_count,
                        json.dumps(params))

            # when min_date_start_for_job stays None, we should
            # still update the bookmark using 'until' in time_ranges
            if min_date_start_for_job is None:
                for time_range in params['time_ranges']:
                    if time_range['until']:
                        min_date_start_for_job = time_range['until']
            yield {'state': advance_bookmark(self, self.bookmark_key,
                                             min_date_start_for_job)}  # pylint: disable=no-member


INSIGHTS_BREAKDOWNS_OPTIONS = {
    'accounts_insights': {
        "breakdowns": []
    },
    'ads_insights': {
        "breakdowns": []
    },
    'ads_insights_age_gender': {
        "breakdowns": ['age', 'gender'],
        "primary-keys": ['age', 'gender']
    },
    'ads_insights_device_platform': {
        'breakdowns': ['device_platform'],
        'primary-keys': ['device_platform']
    },
    'ads_insights_placement': {
        "breakdowns": ['publisher_platform', 'platform_position', 'impression_device'],
        "primary-keys": ['publisher_platform', 'platform_position', 'impression_device']
    },

    # not checked and not used
    'ads_insights_age_and_gender': {"breakdowns": ['age', 'gender'],
                                    "primary-keys": ['age', 'gender']},
    'ads_insights_country': {"breakdowns": ['country']},
    'ads_insights_platform_and_device': {"breakdowns": ['publisher_platform',
                                                        'platform_position', 'impression_device'],
                                         "primary-keys": ['publisher_platform',
                                                          'platform_position', 'impression_device']},
    'ads_insights_region': {'breakdowns': ['region'],
                            'primary-keys': ['region']},
    'ads_insights_dma': {"breakdowns": ['dma'],
                         "primary-keys": ['dma']},
}


def initialize_stream(account, catalog_entry, state):  # pylint: disable=too-many-return-statements

    name = catalog_entry.stream
    stream_alias = catalog_entry.stream_alias

    if name in INSIGHTS_BREAKDOWNS_OPTIONS:
        if name == 'accounts_insights':
            level = 'account'
        else:
            level = 'ad'
        return AdsInsights(name, account, stream_alias, catalog_entry, state=state, level=level,
                           options=INSIGHTS_BREAKDOWNS_OPTIONS[name])
    elif name == 'campaigns':
        return Campaigns(name, account, stream_alias, catalog_entry, state=state)
    elif name == 'adsets':
        return AdSets(name, account, stream_alias, catalog_entry, state=state)
    elif name == 'ads':
        return Ads(name, account, stream_alias, catalog_entry, state=state)
    elif name == 'adcreative':
        return AdCreative(name, account, stream_alias, catalog_entry, state=state)
    elif name == 'adaccounts':
        return AdAccounts(name, account, stream_alias, catalog_entry, state=state)
    else:
        raise TapFacebookException('Unknown stream {}'.format(name))


def get_streams_to_sync(account, catalog, state):
    streams = []
    for stream in STREAMS:
        catalog_entry = next((s for s in catalog.streams if s.tap_stream_id == stream), None)
        if catalog_entry and catalog_entry.is_selected():
            # TODO: Don't need name and stream_alias since it's on catalog_entry
            name = catalog_entry.stream
            stream_alias = catalog_entry.stream_alias
            streams.append(initialize_stream(account, catalog_entry, state))
    return streams


def transform_date_hook(data, typ, schema):
    if typ == 'string' and schema.get('format') == 'date-time' and isinstance(data, str):
        transformed = transform_datetime_string(data)
        return transformed
    return data


def do_sync(account, catalog, state):
    streams_to_sync = get_streams_to_sync(account, catalog, state)
    refs = load_shared_schema_refs()
    for stream in streams_to_sync:
        is_sync_success = False
        try:
            LOGGER.info('Syncing %s | user: %s | account: %s | fields: %s',
                        stream.name,
                        CONFIG['user_id'],
                        account.get_id() if account else '',
                        stream.fields())
            schema = singer.resolve_schema_references(load_schema(stream), refs)
            metadata_map = metadata.to_map(stream.catalog_entry.metadata)
            bookmark_key = BOOKMARK_KEYS.get(stream.name)
            singer.write_schema(stream.name, schema, stream.key_properties, bookmark_key, stream.stream_alias)

            endpoint_tag = {
                'type': stream.name,
                'user': CONFIG['user_id'],
                'account': account.get_id() if account else '',
            }
            with Transformer(pre_hook=transform_date_hook) as transformer:
                with metrics.record_counter(endpoint_tag) as counter:
                    for message in stream:
                        if 'record' in message:
                            counter.increment()
                            time_extracted = utils.now()
                            record = transformer.transform(message['record'], schema, metadata=metadata_map)
                            singer.write_record(stream.name, record, stream.stream_alias, time_extracted)
                        elif 'state' in message:
                            singer.write_state(message['state'])
                        else:
                            raise TapFacebookException('Unrecognized message {}'.format(message))
            is_sync_success = True

        except FacebookError as fb_error:
            info = {
                'timestamp': int(time.time()),
                'type': stream.name,
                'action': 'do_sync',
                'user': CONFIG['user_id'],
                'account': account.get_id() if account else '',
            }
            raise_from(MySingerSyncError, fb_error, info)

        finally:
            info = {
                'timestamp': int(time.time()),
                'type': stream.name,
                'action': 'sync_result',
                'user': CONFIG['user_id'],
                'account': account.get_id() if account else '',
                'count': RECORD_COUNT,
            }
            if stream.name.startswith('ads_insights'):
                info['useful_count'] = USEFUL_RECORD_COUNT
            if is_sync_success:
                LOGGER.info('SYNC_SUCCESS %s' % json.dumps(info))
            else:
                LOGGER.info('SYNC_FAILURE %s' % json.dumps(info))


def get_abs_path(path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)


def load_schema(stream):
    path = get_abs_path('schemas/{}.json'.format(stream.name))
    field_class = stream.field_class
    schema = utils.load_json(path)

    for k in schema['properties']:
        if k not in field_class.__dict__:
            LOGGER.warning(
                'Property %s.%s is not defined in the facebook_business library',
                stream.name, k)

    return schema


def initialize_streams_for_discovery(): # pylint: disable=invalid-name
    return [initialize_stream(None, CatalogEntry(stream=name), None)
            for name in STREAMS]


def discover_schemas():
    # Load Facebook's shared schemas
    refs = load_shared_schema_refs()

    result = {'streams': []}
    streams = initialize_streams_for_discovery()
    for stream in streams:
        LOGGER.info('Loading schema for %s', stream.name)
        schema = singer.resolve_schema_references(load_schema(stream), refs)

        mdata = metadata.to_map(metadata.get_standard_metadata(schema,
                                               key_properties=stream.key_properties))

        bookmark_key = BOOKMARK_KEYS.get(stream.name)
        if bookmark_key == UPDATED_TIME_KEY:
            mdata = metadata.write(mdata, ('properties', bookmark_key), 'inclusion', 'automatic')

        result['streams'].append({'stream': stream.name,
                                  'tap_stream_id': stream.name,
                                  'schema': schema,
                                  'metadata': metadata.to_list(mdata)})
    return result


def load_shared_schema_refs():
    shared_schemas_path = get_abs_path('schemas/shared')

    shared_file_names = [f for f in os.listdir(shared_schemas_path)
                         if os.path.isfile(os.path.join(shared_schemas_path, f))]

    shared_schema_refs = {}
    for shared_file in shared_file_names:
        with open(os.path.join(shared_schemas_path, shared_file)) as data_file:
            shared_schema_refs[shared_file] = json.load(data_file)

    return shared_schema_refs


def do_discover():
    LOGGER.info('Loading schemas')
    json.dump(discover_schemas(), sys.stdout, indent=4)


def main_impl():
    args = None
    account = None
    try:
        args = utils.parse_args(REQUIRED_CONFIG_KEYS)
        account_id = args.config['account_id']
        access_token = args.config['access_token']

        CONFIG.update(args.config)

        global RESULT_RETURN_LIMIT
        RESULT_RETURN_LIMIT = CONFIG.get('result_return_limit', RESULT_RETURN_LIMIT)

        global API
        API = FacebookAdsApi.init(access_token=access_token)
        # do not traversal all accounts to reduce resource usage on over API rate limiting
        if account_id == '':
            global FB_USER
            try:
                FB_USER = fb_user.User(CONFIG['user_id'], api=API)
                FB_USER.get_accounts()
            except FacebookError as fb_error:
                info = {
                    'timestamp': int(time.time()),
                    'type': 'singer',
                    'action': 'user config error',
                    'user': CONFIG['user_id'],
                    'account': account.get_id() if account else '',
                }
                raise_from(MySingerConfigurationError, fb_error, info)
        else:
            try:
                account = fb_account.AdAccount(account_id, api=API)
                account.get_users()
            except FacebookError as fb_error:
                info = {
                    'timestamp': int(time.time()),
                    'type': 'singer',
                    'action': 'account config error',
                    'user': CONFIG['user_id'],
                    'account': account.get_id() if account else '',
                }
                raise_from(MySingerConfigurationError, fb_error, info)

    except FacebookError as fb_error:
        info = {
            'timestamp': int(time.time()),
            'type': 'singer',
            'action': 'other config error',
        }
        raise_from(SingerConfigurationError, fb_error, info)

    if args.discover:
        try:
            do_discover()
        except FacebookError as fb_error:
            info = {
                'timestamp': int(time.time()),
                'type': 'singer',
                'action': 'discover',
            }
            raise_from(SingerDiscoveryError, fb_error, info)
    elif args.properties:
        catalog = Catalog.from_dict(args.properties)
        do_sync(account, catalog, args.state)
    else:
        LOGGER.info("No properties were selected")


def main():
    try:
        main_impl()
    except TapFacebookException as e:
        LOGGER.critical(e)
        sys.exit(1)
    except Exception as e:
        LOGGER.exception(e)
        for line in str(e).splitlines():
            LOGGER.critical(line)
        raise e
