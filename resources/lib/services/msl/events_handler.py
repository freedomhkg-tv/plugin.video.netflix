# -*- coding: utf-8 -*-
"""
    Copyright (C) 2017 Sebastian Golasch (plugin.video.netflix)
    Copyright (C) 2019 Stefano Gottardo - @CastagnaIT (original implementation module)
    Handle and build Netflix events

    SPDX-License-Identifier: MIT
    See LICENSES/MIT.md for more information.
"""
from __future__ import absolute_import, division, unicode_literals

import random
import threading
import time

try:  # Python 2
    from urllib import urlencode
except ImportError:  # Python 3
    from urllib.parse import urlencode

import xbmc

import resources.lib.cache as cache
from resources.lib import common
from resources.lib.database.db_utils import TABLE_SESSION
from resources.lib.globals import g
from resources.lib.services.msl import event_tag_builder
from resources.lib.services.msl.msl_handler_base import build_request_data, ENDPOINTS

try:
    import Queue as queue
except ImportError:  # Python 3
    import queue

EVENT_START = 'start'      # events/start : Video starts
EVENT_STOP = 'stop'        # events/stop : Video stops
EVENT_KEEP_ALIVE = 'keepAlive'  # events/keepAlive : Update progress status
EVENT_ENGAGE = 'engage'    # events/engage : After user interaction (before stop, on skip, on pause)
EVENT_BIND = 'bind'        # events/bind : ?


class Event(object):
    """Object representing an event request to be processed"""

    STATUS_REQUESTED = 'REQUESTED'
    STATUS_INQUEUE = 'IN_QUEUE'
    STATUS_ERROR = 'ERROR'
    STATUS_SUCCESS = 'SUCCESS'

    def __init__(self, event_data):
        self.event_type = event_data['params']['event']
        common.debug('Event type [{}] added to queue: {}', self.event_type, event_data)
        self.status = self.STATUS_INQUEUE
        self.request_data = event_data
        self.response_data = None
        self.req_attempt = 0

    def get_event_id(self):
        return self.request_data['xid']

    def set_response(self, response):
        self.response_data = response
        common.debug('Event type [{}] response: {}', self.event_type, response)
        # Todo check for possible error in response and set right status
        self.status = self.STATUS_ERROR
        self.status = self.STATUS_SUCCESS

    def is_response_success(self):
        return self.status == self.STATUS_SUCCESS

    def is_attempts_granted(self):
        """Returns True if you can make new request attempts"""
        self.req_attempt += 1
        return True if self.req_attempt <= 3 else False

    def __str__(self):
        return self.event_type


class EventsHandler(threading.Thread):
    """Handle and build Netflix event requests"""

    def __init__(self, chunked_request):
        super(EventsHandler, self).__init__()
        self.chunked_request = chunked_request
        # session_id, app_id are common to all events
        self.session_id = int(time.time()) * 10000 + random.randint(1, 10001)
        self.app_id = None
        self.queue_events = queue.Queue(maxsize=10)
        self.cache_data_events = {}
        self.banned_events_ids = []
        common.register_slot(signal=common.Signals.QUEUE_VIDEO_EVENT, callback=self.callback_event_video_queue)

    def run(self):
        """Monitor and process the event queue"""
        common.debug('[Event queue monitor] Thread started')
        monitor = xbmc.Monitor()
        while not monitor.abortRequested():
            try:
                # Take the first queued item
                event = self.queue_events.get_nowait()
                # Process the request
                continue_queue = self._process_event_request(event)
                if not continue_queue:
                    # Ban future requests from this event id
                    self.banned_events_ids += [event.get_event_id()]
            except queue.Empty:
                pass
            monitor.waitForAbort(0.5)

    def _process_event_request(self, event):
        """Do the event post request"""
        event.status = Event.STATUS_REQUESTED
        # Request attempts can be made up to a maximum of 3 times per event
        while event.is_attempts_granted():
            common.info('Perform event request [{}] (attempt {})', event, event.req_attempt)
            params = {'reqAttempt': event.req_attempt,
                      'reqPriority': 20 if event.event_type == EVENT_START else 0,
                      'reqName': 'events/{}'.format(event)}
            url = ENDPOINTS['events'] + '?' + urlencode(params).replace('%2F', '/')
            try:
                response = self.chunked_request(url, event.request_data, g.get_esn())
                event.set_response(response)
                break
            except Exception as exc:
                common.error('Event request [{}] failed: {}', event, exc)
        if event.event_type == EVENT_STOP:
            self.clear_queue()
        if event.event_type == EVENT_START and not event.is_response_success():
            # If 'start' event was unsuccessful,
            # no longer make any future requests from this event id
            return False
        return True

    def callback_event_video_queue(self, data=None):
        """Callback to add a video event"""
        self.add_event_to_queue(data['event_type'], data['event_data'], data['player_state'])

    def add_event_to_queue(self, event_type, event_data, player_state):
        """Adds an event in the queue of events to be processed"""
        videoid = common.VideoId.from_dict(event_data['videoid'])
        previous_data = self.cache_data_events.get(videoid.value, {})
        manifest = get_manifest(videoid)
        url = manifest['links']['events']['href']

        if previous_data.get('xid') in self.banned_events_ids:
            common.warn('Event [{}] not added, is banned for a previous request event error',
                        event_type)
            return

        event_data = build_request_data(url, self._build_event_params(event_type,
                                                                      event_data,
                                                                      player_state,
                                                                      manifest))
        try:
            self.queue_events.put_nowait(Event(event_data))
        except queue.Full:
            common.warn('Events queue is full, event [{}] not queued', event_type)

    def clear_queue(self):
        """Clear all queued events"""
        with self.queue_events.mutex:
            self.queue_events.queue.clear()
        self.cache_data_events = {}
        self.banned_events_ids = []

    def _build_event_params(self, event_type, event_data, player_state, manifest):
        """Build data params for an event request"""
        videoid = common.VideoId.from_dict(event_data['videoid'])
        # Get previous elaborated data of the same video id
        # Some tags must remain unchanged between events
        previous_data = self.cache_data_events.get(videoid.value, {})
        timestamp = int(time.time() * 10000)

        # Context location values can be easily viewed from tag data-ui-tracking-context
        # of a preview box in website html
        play_ctx_location = 'WATCHNOW'  # We currently leave a fixed value, we leave support for future changes
        # play_ctx_location = 'MyListAsGallery' if event_data['is_in_mylist'] else 'browseTitles'

        # To now it is not mandatory, we leave support for future changes
        # if event_data['is_played_by_library']:
        #     list_id = 'unknown'
        # else:
        #     list_id = g.LOCAL_DB.get_value('last_menu_id', 'unknown')

        params = {
            'event': event_type,
            'xid': previous_data.get('xid', str(timestamp + 1610)),
            'position': player_state['elapsed_seconds'] * 1000,  # Video time elapsed
            'clientTime': timestamp,
            'sessionStartTime': previous_data.get('sessionStartTime', timestamp),
            'mediaId': event_tag_builder.get_media_id(videoid, player_state, manifest),
            'trackId': str(event_data['track_id']),
            'sessionId': str(self.session_id),
            'appId': str(self.app_id or self.session_id),
            'playTimes': event_tag_builder.get_play_times(videoid, player_state, manifest),
            'sessionParams': previous_data.get('sessionParams', {
                'isUIAutoPlay': False,  # Should be set equal to the one in the manifest
                'supportsPreReleasePin': True,  # Should be set equal to the one in the manifest
                'supportsWatermark': True,  # Should be set equal to the one in the manifest
                'preferUnletterboxed': True,  # Should be set equal to the one in the manifest
                'uiplaycontext': {
                    # 'list_id': list_id,  # not mandatory
                    # 'lolomo_id': g.LOCAL_DB.get_value('lolomo_root_id', '', TABLE_SESSION),  # not mandatory
                    'location': play_ctx_location,
                    'rank': 0,  # Perhaps this is a reference of cdn rank used in the manifest? (we use always 0)
                    'request_id': event_data['request_id'],
                    'row': 0,  # Purpose not known
                    'track_id': event_data['track_id'],
                    'video_id': videoid.value
                }
            })
        }

        if event_type == EVENT_ENGAGE:
            params['action'] = 'User_Interaction'

        self.cache_data_events[videoid.value] = params
        return params


def get_manifest(videoid):
    """Get the manifest from cache"""
    cache_identifier = g.get_esn() + '_' + videoid.value
    return g.CACHE.get(cache.CACHE_MANIFESTS, cache_identifier, False)