"""NIC.RU (Ru-Center) DNS services manager."""

from __future__ import print_function

import os
import sys
import logging
import textwrap
from xml.etree import ElementTree
from typing import Callable, List, Tuple, Optional

import requests
from requests_oauthlib import OAuth2Session
from oauthlib.oauth2 import LegacyApplicationClient, TokenExpiredError, InvalidGrantError

from nic_api.models import (
    parse_record,
    NICService,
    NICZone,
    DNSRecord,
    SOARecord,
    NSRecord,
    ARecord,
    AAAARecord,
    CNAMERecord,
    MXRecord,
    TXTRecord,
)
from nic_api.exceptions import DnsApiException


_RECORD_CLASSES_CAN_ADD = (
    ARecord,
    AAAARecord,
    CNAMERecord,
    TXTRecord,
)


def is_sequence(arg):
    """Returns if argument is list/tuple/etc. or not."""
    return (not hasattr(arg, 'strip') and
            hasattr(arg, '__getitem__') or
            hasattr(arg, '__iter__'))


def pprint(record):
    """Pretty print for DNS records."""
    _format_default = '{:45} {:6} {:6} {}'
    _format_mx = '{:45} {:6} {:6} {:4} {}'
    _format_soa = textwrap.dedent("""\
                  {name:30} IN SOA {mname} {rname} (
                  {serial:>50} ; Serial
                  {refresh:>50} ; Refresh
                  {retry:>50} ; Retry
                  {expire:>50} ; Expire
                  {minimum:>50})""")

    if isinstance(record, ARecord):
        print(_format_default.format(
            record.name,
            record.ttl if record.ttl is not None else '',
            'A',
            record.a,
        ))
    elif isinstance(record, AAAARecord):
        print(_format_default.format(
            record.name,
            record.ttl if record.ttl is not None else '',
            'AAAA',
            record.aaaa,
        ))
    elif isinstance(record, CNAMERecord):
        print(_format_default.format(
            record.name, record.ttl, 'CNAME', record.cname))
    elif isinstance(record, MXRecord):
        print(_format_mx.format(
            record.name, record.ttl, 'MX', record.preference, record.exchange))
    elif isinstance(record, TXTRecord):
        print(_format_default.format(
            record.name, record.ttl, 'TXT', record.txt))
    elif isinstance(record, NSRecord):
        print(_format_default.format(record.name, ' ', 'NS', record.ns))
    elif isinstance(record, SOARecord):
        print(_format_soa.format(
            name=record.name,
            mname=record.mname.name,
            rname=record.rname.name,
            serial=record.serial,
            refresh=record.refresh,
            retry=record.retry,
            expire=record.expire,
            minimum=record.minimum
        ))
    else:
        print(record)
        print('Unknown record type: {}'.format(type(record)))


class DnsApi(object):
    """Class for managing NIC.RU DNS services by API.

    Username and password are required only if it fails to authorize with
    cached token.

    Arguments:
        oauth_config: a dict with OAuth app credentials;
        default_service: a default name of NIC service to use in API calls;
        default_zone: a default DNS zone to use in API calls;


    oauth_config should contain the application login and the password.
    Example:

        {'APP_LOGIN': 'aaaaaa', 'APP_PASSWORD': 'bbbbb'}

    You can obtain these credentials at the NIC.RU application authorization
    page: https://www.nic.ru/manager/oauth.cgi?step=oauth.app_register
    """

    base_url_default = "https://api.nic.ru"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        username: str = None,
        password: str = None,
        scope: str = None,
        token: str = None,
        default_service: str = None,
        default_zone: str = None,
        token_updater: Callable[[dict], None] = None,
        timeout: int = 600,
        base_url: str = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._username = username
        self._password = password
        self._scope = scope
        self._token = token
        self._base_url = self.base_url_default if base_url is None else base_url
        self._token_updater_clb = token_updater
        self._timeout = timeout
        self.default_service = default_service
        self.default_zone = default_zone

        # Logging setup
        self.logger = logging.getLogger(__name__)

        # Setup
        self._session = OAuth2Session(
            client=LegacyApplicationClient(
                client_id=self._client_id, scope=self._scope
            ),
            auto_refresh_url=self.url_token,
            auto_refresh_kwargs={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            token_updater=self._token_updater,
            token=self._token
        )

    @property
    def url_token(self):
        return f"{self._base_url}/oauth/token"

    def _token_updater(self, token: dict):
        self._token = token
        if self._token_updater_clb is not None:
            self._token_updater_clb(token)

    def get_token(self):
        """Get token"""
        try:
            token = self._session.fetch_token(
                token_url=self.url_token,
                username=self._username,
                password=self._password,
                client_id=self._client_id,
                client_secret=self._client_secret,
            )
        except InvalidGrantError as e:
            raise DnsApiException(str(e))
        self._token_updater(token)

    def _url_create(self, rpath: str) -> str:
        return f"{self._base_url}/dns-master{rpath}"

    def _request_data(
        self,
        *args,
        data_as_list: bool = False,
        data_none_except: bool = False,
        **kwargs
    ) -> ElementTree.Element:
        response = self._request(*args, **kwargs)
        status, error, data = self._parse_answer(response.text)
        if status != "success":
            raise DnsApiException(error)
        if data_as_list:
            data = [] if data is None else data
        if data_none_except and data is None:
            raise DnsApiException(
                f"Can't find <data> in response: {response.text}")
        return data

    def _request(
        self,
        method: str,
        rpath: str,
        check_status: bool = False,
        **kwargs
    ) -> requests.Response:
        response = self._session.request(
            method, self._url_create(rpath), timeout=self._timeout, **kwargs)

        # Check http error
        if check_status and not response.ok:
            raise DnsApiException(f"HTTP Error. Body: {response.text}")
        
        return response
   
    def _parse_answer(
            self, body: str
        ) -> Tuple[str, str, Optional[ElementTree.Element]]:
        """Gets <data> from XML response.

        Arguments:
            body - xml as text

        Returns:
            (xml.etree.ElementTree.Element) <data> tag of response.
        """
        root = ElementTree.fromstring(body)
        data = root.find('data')

        status = root.find('status')
        if status is None:
            raise DnsApiException(f"Can't find <status> in response: {body}")
        status = status.text

        error = ""
        for item in root.findall('errors/error'):
            error += " Code: {}. {}".format(
                item.attrib.get("code", ""), item.text)

        return status, error.strip(), data

    def services(self) -> List[NICService]:
        """Get services available for management.

        Returns:
            a list of NICService objects.
        """
        data = self._request_data("GET", "/services", data_as_list=True)
        return [NICService.from_xml(service) for service in data]

    def zones(self, service: str = None) -> List[NICZone]:
        """Get zones in service.

        Returns:
            a list of NICZone objects.
        """
        service = self.default_service if service is None else service
        if service is None:
            rpath = "/zones"
        else:
            rpath = f"/services/{service}/zones"
        data = self._request_data("GET", rpath, data_as_list=True)
        return [NICZone.from_xml(zone) for zone in data]

    def zonefile(self, service: str = None, zone: str = None) -> str:
        """Get zone file for single zone.

        Returns:
            a string with zonefile content.
        """
        service = self.default_service if service is None else service
        zone = self.default_zone if zone is None else zone
        response = self._request(
            "GET", f"/services/{service}/zones/{zone}", check_status=True)
        return response.text

    def records(self, service: str = None, zone: str = None) -> List[DNSRecord]:
        """Get all records for single zone.

        Returns:
            a list with DNSRecord subclasses objects.
        """
        service = self.default_service if service is None else service
        zone = self.default_zone if zone is None else zone
        data = self._request_data(
            "GET",
            f"/services/{service}/zones/{zone}/records",
            data_none_except=True
        )
        _zone = data.find('zone')
        assert _zone.attrib['name'] == zone
        return [parse_record(rr) for rr in _zone.findall('rr')]

    def add_record(self, records, service: str = None, zone: str = None):
        """Adds records."""
        service = self.default_service if service is None else service
        zone = self.default_zone if zone is None else zone
        if not is_sequence(records):
            _records = [records]
        else:
            _records = list(records)

        rr_list = []  # for XML representations

        for record in _records:
            if not isinstance(record, _RECORD_CLASSES_CAN_ADD):
                raise TypeError('{} is not a valid DNS record!'.format(record))
            record_xml = record.to_xml()
            rr_list.append(record_xml)
            self.logger.debug('Prepared for addition new record on service %s'
                              ' zone %s: %s', service, zone, record_xml)

        _xml = textwrap.dedent(
            """\
            <?xml version="1.0" encoding="UTF-8" ?>
            <request><rr-list>
            {}
            </rr-list></request>"""
        ).format('\n'.join(rr_list))

        self._request_data(
            "PUT",
            f"/services/{service}/zones/{zone}/records",
            data=_xml
        )
        self.logger.debug('Successfully added %s records', len(rr_list))

    def delete_record(
        self,
        record_id: int,
        service: str = None,
        zone: str = None
    ):
        """Deletes record by id."""
        service = self.default_service if service is None else service
        zone = self.default_zone if zone is None else zone

        self.logger.debug(
            'Deleting record #%s on service %s zone %s',
            record_id,
            service,
            zone
        )

        self._request_data(
            "DELETE", f"/services/{service}/zones/{zone}/records/{record_id}")

        self.logger.debug('Record #%s deleted!', record_id)

    def commit(self, service: str = None, zone: str = None):
        """Commits changes in zone."""
        service = self.default_service if service is None else service
        zone = self.default_zone if zone is None else zone
        self._request_data(
            "POST", f"/services/{service}/zones/{zone}/commit")
        self.logger.debug('Changes committed!')

    def rollback(self, service: str = None, zone: str = None):
        """Rolls back changes in zone."""
        service = self.default_service if service is None else service
        zone = self.default_zone if zone is None else zone
        self._request_data(
            "POST", f"/services/{service}/zones/{zone}/rollback")
        self.logger.debug('Changes are rolled back!')

# vim: ts=4:sw=4:et:sta:si
