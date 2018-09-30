# -*- coding: utf-8 -*-
"""Main module."""
import logging
from datetime import timedelta
from typing import Optional, Dict, Any

import attr
from aiohttp import hdrs, ClientSession
from multidict import MultiDict
from yarl import URL

READY_STATE_CONNECTING = 0
READY_STATE_OPEN = 1
READY_STATE_CLOSED = 2

DEFAULT_RECONNECTION_TIME = timedelta(seconds=5)

CONTENT_TYPE_EVENT_STREAM = 'text/event-stream'

_LOGGER = logging.getLogger(__name__)


@attr.s(slots=True, frozen=True)
class MessageEvent:
    """Represent DOM MessageEvent Interface

    .. seealso:: https://www.w3.org/TR/eventsource/#dispatchMessage section 4
    .. seealso:: https://developer.mozilla.org/en-US/docs/Web/API/MessageEvent
    """
    type = attr.ib(type=str)
    message = attr.ib(type=str)
    data = attr.ib(type=str)
    origin = attr.ib(type=str)
    last_event_id = attr.ib(type=str)


class EventSource:
    """Represent EventSource Interface as an async context manager.

    .. seealso:: https://www.w3.org/TR/eventsource/#eventsource
    """
    def __init__(self, url: str,
                 option: Optional[Dict[str, Any]] = None,
                 reconnection_time: timedelta = DEFAULT_RECONNECTION_TIME,
                 session: Optional[ClientSession] = None,
                 **kwargs):
        """Construct EventSource instance.

        :param url: specifies the URL to which to connect
        :param option: specifies the settings, if any,
            in the form of an Dict[str, Any]. Current only one key supported
            - with_credentials: bool, specifies CORS mode to `Use Credentials`
        :param reconnection_time: wait time before try to reconnect in case
            connection broken
        :param session: specifies a aiohttp.ClientSession, if not, create
            a default ClientSession
        """
        self._url = URL(url)
        if option is not None:
            self._with_credentials = option.get('with_credentials', False)
        else:
            self._with_credentials = False
        self._ready_state = READY_STATE_CONNECTING

        if session is not None:
            self._session = session
            self._need_close_session = False
        else:
            self._session = ClientSession()
            self._need_close_session = True

        self._reconnection_time = reconnection_time
        self._last_event_id = ''
        self._kwargs = kwargs or {'headers': MultiDict()}

        self._event_id = ''
        self._event_type = ''
        self._event_data = ''

        self._origin = None
        self._response = None

    def __enter__(self):
        """Use async with instead."""
        raise TypeError("Use async with instead")

    def __exit__(self, *exc):
        """Should exist in pair with __enter__ but never executed."""
        pass  # pragma: no cover

    async def __aenter__(self) -> 'EventSource':
        """Connect and listen Server-Sent Event."""
        await self._connect()
        return self

    async def __aexit__(self, *exc):
        """Close connection."""
        if self._need_close_session:
            await self._session.close()
        pass

    @property
    def url(self) -> URL:
        """Return URL to which to connect."""
        return self._url

    @property
    def with_credentials(self) -> bool:
        """Return whether CORS mode set to `User Credentials`."""
        return self._with_credentials

    @property
    def ready_state(self) -> int:
        """Return ready state."""
        return self._ready_state

    def __aiter__(self):
        """Return"""
        return self

    async def __anext__(self) -> MessageEvent:
        """Process events"""
        if not self._response:
            raise ValueError

        # async for ... in StreamReader only split line by \n
        async for line_in_bytes in self._response.content:
            line = line_in_bytes.decode('utf8')  # type: str
            line = line.rstrip('\n').rstrip('\r')

            if line == '':
                # empty line
                event = self._dispatch_event()
                if event is not None:
                    return event
                continue

            if line[0] == ':':
                # comment line, ignore
                continue

            if ':' in line:
                # contains ':'
                fields = line.split(':', 1)
                field_name = fields[0]
                field_value = fields[1].lstrip(' ')
                self._process_field(field_name, field_value)
            else:
                self._process_field(line, '')

    async def _connect(self):
        """Connect to resource."""
        _LOGGER.debug('_connect')
        headers = self._kwargs['headers']

        # For HTTP connections, the Accept header may be included;
        # if included, it must contain only formats of event framing that are
        # supported by the user agent (one of which must be text/event-stream,
        # as described below).
        headers[hdrs.ACCEPT] = CONTENT_TYPE_EVENT_STREAM

        # If the event source's last event ID string is not the empty string,
        # then a Last-Event-ID HTTP header must be included with the request,
        # whose value is the value of the event source's last event ID string,
        # encoded as UTF-8.
        headers['Last-Event_ID'] = self._last_event_id

        # User agents should use the Cache-Control: no-cache header in
        # requests to bypass any caches for requests of event sources.
        headers[hdrs.CACHE_CONTROL] = 'no-cache'

        response = await self._session.get(self._url, **self._kwargs)
        if response.status >= 400:
            # TODO: error handle
            _LOGGER.error('fetch %s failed: %s', self._url, response.status)
            return
        # if response.headers.get(hdrs.CONTENT_TYPE) != \
        #         CONTENT_TYPE_EVENT_STREAM:
        #     # TODO: error handle
        #     _LOGGER.error(
        #         'fetch %s failed with wrong Content-Type: %s', self._url,
        #         response.headers.get(hdrs.CONTENT_TYPE))
        #     return

        await self._connected()

        self._response = response
        self._origin = str(response.real_url.origin())

    async def _connected(self):
        """Announce the connection is made."""
        if self._ready_state != READY_STATE_CLOSED:
            self._ready_state = READY_STATE_OPEN
            # TODO: fire open event
            _LOGGER.debug('open event')
        pass

    def _dispatch_event(self):
        """Dispatch event."""
        self._last_event_id = self._event_id

        if self._event_data == '':
            self._event_type = ''
            return

        self._event_data = self._event_data.rstrip('\n')

        message = MessageEvent(
            type=self._event_type if self._event_type != '' else None,
            message=self._event_type,
            data=self._event_data,
            origin=self._origin,
            last_event_id=self._last_event_id
        )
        # TODO: fire event
        _LOGGER.debug(message)

        self._event_type = ''
        self._event_data = ''
        return message

    def _process_field(self, field_name, field_value):
        """Process field."""
        if field_name == 'event':
            self._event_type = field_value

        elif field_name == 'data':
            self._event_data += field_value
            self._event_data += '\n'

        elif field_name == 'id':
            self._event_id = field_value

        elif field_name == 'retry':
            try:
                retry_in_ms = int(field_value)
                self._reconnection_time = timedelta(milliseconds=retry_in_ms)
            except ValueError:
                _LOGGER.warning('Received invalid retry value %s, ignore it',
                                field_value)
                pass

        pass
