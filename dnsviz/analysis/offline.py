#
# This file is a part of DNSViz, a tool suite for DNS/DNSSEC monitoring,
# analysis, and visualization.  This file (or some portion thereof) is a
# derivative work authored by VeriSign, Inc., and created in 2014, based on
# code originally developed at Sandia National Laboratories.
# Created by Casey Deccio (casey@deccio.net)
#
# Copyright 2012-2014 Sandia Corporation. Under the terms of Contract
# DE-AC04-94AL85000 with Sandia Corporation, the U.S. Government retains
# certain rights in this software.
# 
# Copyright 2014-2015 VeriSign, Inc.
# 
# DNSViz is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# DNSViz is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

import collections
import errno
import logging

import dns.flags, dns.rdataclass, dns.rdatatype

from dnsviz import crypto 
import dnsviz.format as fmt
import dnsviz.query as Q
from dnsviz.response import DNSKEYMeta
from dnsviz.util import tuple_to_dict

import errors as Errors
from online import OnlineDomainNameAnalysis
import status as Status

_logger = logging.getLogger(__name__)

class FoundYXDOMAIN(Exception):
    pass

class OfflineDomainNameAnalysis(OnlineDomainNameAnalysis):
    RDTYPES_ALL = 0
    RDTYPES_ALL_SAME_NAME = 1
    RDTYPES_NS_TARGET = 2
    RDTYPES_SECURE_DELEGATION = 3
    RDTYPES_DELEGATION = 4

    QUERY_CLASS = Q.MultiQueryAggregateDNSResponse

    def __init__(self, name, stub=False):
        super(OfflineDomainNameAnalysis, self).__init__(name, stub=stub)

        # Shortcuts to the values in the SOA record.
        self.serial = None
        self.rname = None
        self.mname = None

        self.dnssec_algorithms_in_dnskey = set()
        self.dnssec_algorithms_in_ds = set()
        self.dnssec_algorithms_in_dlv = set()
        self.dnssec_algorithms_digest_in_ds = set()
        self.dnssec_algorithms_digest_in_dlv = set()

        self.status = None
        self.yxdomain = None
        self.yxrrset = None
        self.nxrrset = None
        self.rrset_warnings = None
        self.rrset_errors = None
        self.rrsig_status = None
        self.wildcard_status = None
        self.dname_status = None
        self.nxdomain_status = None
        self.nxdomain_warnings = None
        self.nxdomain_errors = None
        self.nodata_status = None
        self.nodata_warnings = None
        self.nodata_errors = None
        self.response_errors = None

        self.ds_status_by_ds = None
        self.ds_status_by_dnskey = None

        self.delegation_warnings = None
        self.delegation_errors = None
        self.delegation_status = None

        self.published_keys = None
        self.revoked_keys = None
        self.zsks = None
        self.ksks = None

    def _signed(self):
        return bool(self.dnssec_algorithms_in_dnskey or self.dnssec_algorithms_in_ds or self.dnssec_algorithms_in_dlv)
    signed = property(_signed)

    def _handle_soa_response(self, rrset):
        '''Indicate that there exists an SOA record for the name which is the
        subject of this analysis, and save the relevant parts.'''

        self.has_soa = True
        if self.serial is None or rrset[0].serial > self.serial:
            self.serial = rrset[0].serial
            self.rname = rrset[0].rname
            self.mname = rrset[0].mname

    def _handle_dnskey_response(self, rrset):
        for dnskey in rrset:
            self.dnssec_algorithms_in_dnskey.add(dnskey.algorithm)

    def _handle_ds_response(self, rrset):
        if rrset.rdtype == dns.rdatatype.DS:
            dnssec_algs = self.dnssec_algorithms_in_ds
            digest_algs = self.dnssec_algorithms_digest_in_ds
        else:
            dnssec_algs = self.dnssec_algorithms_in_dlv
            digest_algs = self.dnssec_algorithms_digest_in_dlv
        for ds in rrset:
            dnssec_algs.add(ds.algorithm)
            digest_algs.add((ds.algorithm, ds.digest_type))

    def _process_response_answer_rrset(self, rrset, query, response):
        super(OfflineDomainNameAnalysis, self)._process_response_answer_rrset(rrset, query, response)
        if query.qname in (self.name, self.dlv_name):
            if rrset.rdtype == dns.rdatatype.SOA:
                self._handle_soa_response(rrset)
            elif rrset.rdtype == dns.rdatatype.DNSKEY:
                self._handle_dnskey_response(rrset)
            elif rrset.rdtype in (dns.rdatatype.DS, dns.rdatatype.DLV):
                self._handle_ds_response(rrset)

    def _index_dnskeys(self):
        self._dnskey_sets = []
        self._dnskeys = {}
        if (self.name, dns.rdatatype.DNSKEY) not in self.queries:
            return
        for dnskey_info in self.queries[(self.name, dns.rdatatype.DNSKEY)].answer_info:
            # there are CNAMEs that show up here...
            if not (dnskey_info.rrset.name == self.name and dnskey_info.rrset.rdtype == dns.rdatatype.DNSKEY):
                continue
            dnskey_set = set()
            for dnskey_rdata in dnskey_info.rrset:
                if dnskey_rdata not in self._dnskeys:
                    self._dnskeys[dnskey_rdata] = DNSKEYMeta(dnskey_info.rrset.name, dnskey_rdata, dnskey_info.rrset.ttl)
                self._dnskeys[dnskey_rdata].rrset_info.append(dnskey_info)
                self._dnskeys[dnskey_rdata].servers_clients.update(dnskey_info.servers_clients)
                dnskey_set.add(self._dnskeys[dnskey_rdata])

            self._dnskey_sets.append((dnskey_set, dnskey_info))

    def get_dnskey_sets(self):
        if not hasattr(self, '_dnskey_sets') or self._dnskey_sets is None:
            self._index_dnskeys()
        return self._dnskey_sets

    def get_dnskeys(self):
        if not hasattr(self, '_dnskeys') or self._dnskeys is None:
            self._index_dnskeys()
        return self._dnskeys.values()

    def potential_trusted_keys(self):
        active_ksks = self.ksks.difference(self.zsks).difference(self.revoked_keys)
        if active_ksks:
            return active_ksks
        return self.ksks.difference(self.revoked_keys)

    def _rdtypes_for_analysis_level(self, level):
        rdtypes = set([self.referral_rdtype, dns.rdatatype.NS])
        if level == self.RDTYPES_DELEGATION:
            return rdtypes
        rdtypes.update([dns.rdatatype.DNSKEY, dns.rdatatype.DS, dns.rdatatype.DLV])
        if level == self.RDTYPES_SECURE_DELEGATION:
            return rdtypes
        rdtypes.update([dns.rdatatype.A, dns.rdatatype.AAAA])
        if level == self.RDTYPES_NS_TARGET:
            return rdtypes
        return None

    def _server_responsive_with_condition(self, server, client, request_test, response_test):
        for query in self.queries.values():
            for query1 in query.queries.values():
                if request_test(query1):
                    try:
                        if client is None:
                            clients = query1.responses[server].keys()
                        else:
                            clients = (client,)
                    except KeyError:
                        continue

                    for c in clients:
                        try:
                            response = query1.responses[server][client]
                        except KeyError:
                            continue
                        if response_test(response):
                            return True
        return False

    def server_responsive_with_edns_flag(self, server, client, f):
        return self._server_responsive_with_condition(server, client,
                lambda x: x.edns >= 0 and x.edns_flags & f,
                lambda x: ((x.effective_tcp and x.tcp_responsive) or \
                        (not x.effective_tcp and x.udp_responsive)) and \
                        x.effective_edns >= 0 and x.effective_edns_flags & f)

    def server_responsive_valid_with_edns_flag(self, server, client, f):
        return self._server_responsive_with_condition(server, client,
                lambda x: x.edns >= 0 and x.edns_flags & f,
                lambda x: x.is_valid_response() and \
                        x.effective_edns >= 0 and x.effective_edns_flags & f)

    def server_responsive_with_do(self, server, client):
        return self.server_responsive_with_edns_flag(server, client, dns.flags.DO)

    def server_responsive_valid_with_do(self, server, client):
        return self.server_responsive_valid_with_edns_flag(server, client, dns.flags.DO)

    def server_responsive_with_edns(self, server, client):
        return self._server_responsive_with_condition(server, client,
                lambda x: x.edns >= 0,
                lambda x: ((x.effective_tcp and x.tcp_responsive) or \
                        (not x.effective_tcp and x.udp_responsive)) and \
                        x.effective_edns >= 0)

    def server_responsive_valid_with_edns(self, server, client):
        return self._server_responsive_with_condition(server, client,
                lambda x: x.edns >= 0,
                lambda x: x.is_valid_response() and \
                        x.effective_edns >= 0)

    def populate_status(self, trusted_keys, supported_algs=None, supported_digest_algs=None, is_dlv=False, level=RDTYPES_ALL, trace=None, follow_mx=True):
        if trace is None:
            trace = []

        # avoid loops
        if self in trace:
            self._populate_name_status(level)
            return

        # if status has already been populated, then don't reevaluate
        if self.rrsig_status is not None:
            return

        # if we're a stub, there's nothing to evaluate
        if self.stub:
            return

        # identify supported algorithms as intersection of explicitly supported
        # and software supported
        if supported_algs is not None:
            supported_algs.intersection_update(crypto._supported_algs)
        else:
            supported_algs = crypto._supported_algs
        if supported_digest_algs is not None:
            supported_digest_algs.intersection_update(crypto._supported_digest_algs)
        else:
            supported_digest_algs = crypto._supported_digest_algs

        # populate status of dependencies
        if level <= self.RDTYPES_NS_TARGET:
            for cname in self.cname_targets:
                for target, cname_obj in self.cname_targets[cname].items():
                    cname_obj.populate_status(trusted_keys, level=max(self.RDTYPES_ALL_SAME_NAME, level), trace=trace + [self])
            if follow_mx:
                for target, mx_obj in self.mx_targets.items():
                    if mx_obj is not None:
                        mx_obj.populate_status(trusted_keys, level=max(self.RDTYPES_ALL_SAME_NAME, level), trace=trace + [self], follow_mx=False)
        if level <= self.RDTYPES_SECURE_DELEGATION:
            for signer, signer_obj in self.external_signers.items():
                if signer_obj is not None:
                    signer_obj.populate_status(trusted_keys, level=self.RDTYPES_SECURE_DELEGATION, trace=trace + [self])
            for target, ns_obj in self.ns_dependencies.items():
                if ns_obj is not None:
                    ns_obj.populate_status(trusted_keys, level=self.RDTYPES_NS_TARGET, trace=trace + [self])

        # populate status of ancestry
        if self.parent is not None:
            self.parent.populate_status(trusted_keys, supported_algs, supported_digest_algs, level=self.RDTYPES_SECURE_DELEGATION, trace=trace + [self])
        if self.dlv_parent is not None:
            self.dlv_parent.populate_status(trusted_keys, supported_algs, supported_digest_algs, is_dlv=True, level=self.RDTYPES_SECURE_DELEGATION, trace=trace + [self])

        _logger.debug('Assessing status of %s...' % (fmt.humanize_name(self.name)))
        self._populate_name_status(level)
        if level <= self.RDTYPES_SECURE_DELEGATION:
            self._index_dnskeys()
        self._populate_rrsig_status_all(supported_algs, level)
        self._populate_nodata_status(supported_algs, level)
        self._populate_nxdomain_status(supported_algs, level)
        self._finalize_key_roles()
        if level <= self.RDTYPES_SECURE_DELEGATION:
            if not is_dlv:
                self._populate_delegation_status(supported_algs, supported_digest_algs)
            if self.dlv_parent is not None:
                self._populate_ds_status(dns.rdatatype.DLV, supported_algs, supported_digest_algs)
            self._populate_dnskey_status(trusted_keys)

    def _populate_name_status(self, level, trace=None):
        # using trace allows _populate_name_status to be called independent of
        # populate_status
        if trace is None:
            trace = []

        # avoid loops
        if self in trace:
            return

        self.status = Status.NAME_STATUS_INDETERMINATE
        self.yxdomain = set()
        self.yxrrset = set()
        self.nxrrset = set()

        bailiwick_map, default_bailiwick = self.get_bailiwick_mapping()
        
        required_rdtypes = self._rdtypes_for_analysis_level(level)
        for (qname, rdtype), query in self.queries.items():

            if level > self.RDTYPES_ALL and qname not in (self.name, self.dlv_name):
                continue

            if required_rdtypes is not None and rdtype not in required_rdtypes:
                continue

            qname_obj = self.get_name(qname)
            if rdtype == dns.rdatatype.DS:
                qname_obj = qname_obj.parent
            elif rdtype == dns.rdatatype.DLV:
                qname_obj = qname_obj.dlv_parent

            for rrset_info in query.answer_info:
                self.yxdomain.add(rrset_info.rrset.name)
                self.yxrrset.add((rrset_info.rrset.name, rrset_info.rrset.rdtype))
                if rrset_info.dname_info is not None:
                    self.yxrrset.add((rrset_info.dname_info.rrset.name, rrset_info.dname_info.rrset.rdtype))
                for cname_rrset_info in rrset_info.cname_info_from_dname:
                    self.yxrrset.add((cname_rrset_info.dname_info.rrset.name, cname_rrset_info.dname_info.rrset.rdtype))
                    self.yxrrset.add((cname_rrset_info.rrset.name, cname_rrset_info.rrset.rdtype))
            for neg_response_info in query.nodata_info:
                for (server,client) in neg_response_info.servers_clients:
                    for response in neg_response_info.servers_clients[(server,client)]:
                        if neg_response_info.qname == qname or response.recursion_desired_and_available():
                            if not response.is_upward_referral(qname_obj.zone.name):
                                self.yxdomain.add(neg_response_info.qname)
                            self.nxrrset.add((neg_response_info.qname, neg_response_info.rdtype))
            for neg_response_info in query.nxdomain_info:
                for (server,client) in neg_response_info.servers_clients:
                    for response in neg_response_info.servers_clients[(server,client)]:
                        if neg_response_info.qname == qname or response.recursion_desired_and_available():
                            self.nxrrset.add((neg_response_info.qname, neg_response_info.rdtype))

            if level <= self.RDTYPES_DELEGATION:
                # now check referrals (if name hasn't already been identified as YXDOMAIN)
                if self.name == qname and self.name not in self.yxdomain:
                    if rdtype not in (self.referral_rdtype, dns.rdatatype.NS):
                        continue
                    try:
                        for query1 in query.queries.values():
                            for server in query1.responses:
                                bailiwick = bailiwick_map.get(server, default_bailiwick)
                                for client in query1.responses[server]:
                                    if query1.responses[server][client].is_referral(self.name, rdtype, bailiwick, proper=True):
                                        self.yxdomain.add(self.name)
                                        raise FoundYXDOMAIN
                    except FoundYXDOMAIN:
                        pass

        if level <= self.RDTYPES_NS_TARGET:
            # now add the values of CNAMEs
            for cname in self.cname_targets:
                if level > self.RDTYPES_ALL and cname not in (self.name, self.dlv_name):
                    continue
                for target, cname_obj in self.cname_targets[cname].items():
                    if cname_obj is self:
                        continue
                    if cname_obj.yxrrset is None:
                        cname_obj._populate_name_status(self.RDTYPES_ALL, trace=trace + [self])
                    for name, rdtype in cname_obj.yxrrset:
                        if name == target:
                            self.yxrrset.add((cname,rdtype))

        if self.name in self.yxdomain:
            self.status = Status.NAME_STATUS_NOERROR

        if self.status == Status.NAME_STATUS_INDETERMINATE:
            for (qname, rdtype), query in self.queries.items():
                if rdtype == dns.rdatatype.DS:
                    continue
                if filter(lambda x: x.qname == qname, query.nxdomain_info):
                    self.status = Status.NAME_STATUS_NXDOMAIN
                    break

    def _populate_response_errors(self, qname_obj, response, server, client, warnings, errors):
        # if the initial request used EDNS
        if response.query.edns >= 0:
            err = None
            #TODO check for general intermittent errors (i.e., not just for EDNS/DO)
            #TODO mark a slow response as well (over a certain threshold)

            # if the response didn't use EDNS
            if response.message.edns < 0:
                # if the effective request didn't use EDNS either
                if response.effective_edns < 0:
                    # find out if this really appears to be an EDNS issue, by
                    # seeing if any other queries to this server with EDNS were
                    # actually successful 
                    if response.responsive_cause_index is not None:
                        if response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_NETWORK_ERROR:
                            if qname_obj is not None and qname_obj.zone.server_responsive_with_edns(server,client):
                                err = Errors.NetworkError(tcp=response.query.tcp_first, errno=errno.errorcode.get(response.history[response.responsive_cause_index].cause_arg, 'UNKNOWN'), intermittent=True)
                            else:
                                err = Errors.ResponseErrorWithEDNS(response_error=Errors.NetworkError(tcp=response.query.tcp_first, errno=errno.errorcode.get(response.history[response.responsive_cause_index].cause_arg, 'UNKNOWN'), intermittent=False))
                        elif response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_FORMERR:
                            if qname_obj is not None and qname_obj.zone.server_responsive_valid_with_edns(server,client):
                                err = Errors.FormError(tcp=response.query.tcp_first, msg_size=response.msg_size, intermittent=True)
                            else:
                                err = Errors.ResponseErrorWithEDNS(response_error=Errors.FormError(tcp=response.query.tcp_first, msg_size=response.msg_size, intermittent=False))
                        elif response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_TIMEOUT:
                            if qname_obj is not None and qname_obj.zone.server_responsive_with_edns(server,client):
                                err = Errors.Timeout(tcp=response.query.tcp_first, attempts=response.responsive_cause_index+1, intermittent=True)
                            else:
                                err = Errors.ResponseErrorWithEDNS(response_error=Errors.Timeout(tcp=response.query.tcp_first, attempts=response.responsive_cause_index+1, intermittent=False))
                        elif response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_OTHER:
                            if qname_obj is not None and qname_obj.zone.server_responsive_valid_with_edns(server,client):
                                err = Errors.UnknownResponseError(tcp=response.query.tcp_first, intermittent=True)
                            else:
                                err = Errors.ResponseErrorWithEDNS(response_error=Errors.UnknownResponseError(tcp=response.query.tcp_first, intermittent=False))
                        elif response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_RCODE:
                            if qname_obj is not None and qname_obj.zone.server_responsive_valid_with_edns(server,client):
                                err = Errors.InvalidRcode(tcp=response.query.tcp_first, rcode=dns.rcode.to_text(response.history[response.responsive_cause_index].cause_arg), intermittent=True)
                            else:
                                err = Errors.ResponseErrorWithEDNS(response_error=Errors.InvalidRcode(tcp=response.query.tcp_first, rcode=dns.rcode.to_text(response.history[response.responsive_cause_index].cause_arg), intermittent=False))

                # if the ultimate request used EDNS, then it was simply ignored
                # by the server
                else:
                    err = Errors.EDNSIgnored()

                #TODO handle this better
                if err is None:
                    raise Exception('Unknown EDNS-related error')

            # the response did use EDNS
            else:

                # check for EDNS version mismatch
                if response.message.edns != response.query.edns:
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.UnsupportedEDNSVersion(version=response.query.edns), warnings, server, client, response)

                # check for PMTU issues
                #TODO need bounding here
                if response.effective_edns_max_udp_payload != response.query.edns_max_udp_payload:
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.PMTUExceeded(pmtu_lower_bound=None, pmtu_upper_bound=None), warnings, server, client, response)

                if response.query.edns_flags != response.effective_edns_flags:
                    for i in range(15, -1, -1):
                        f = 1 << i
                        # the response used EDNS with the given flag, but the flag
                        # wasn't (ultimately) requested
                        if ((response.query.edns_flags & f) != (response.effective_edns_flags & f)):
                            # find out if this really appears to be a flag issue,
                            # by seeing if any other queries to this server with
                            # the DO bit were also unsuccessful 
                            if response.responsive_cause_index is not None:
                                if response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_NETWORK_ERROR:
                                    if qname_obj is not None and qname_obj.zone.server_responsive_with_edns_flag(server,client,f):
                                        err = Errors.NetworkError(tcp=response.query.tcp_first, errno=errno.errorcode.get(response.history[response.responsive_cause_index].cause_arg, 'UNKNOWN'), intermittent=True)
                                    else:
                                        err = Errors.ResponseErrorWithEDNSFlag(response_error=Errors.NetworkError(tcp=response.query.tcp_first, errno=errno.errorcode.get(response.history[response.responsive_cause_index].cause_arg, 'UNKNOWN'), intermittent=False), flag=dns.flags.edns_to_text(f))
                                elif response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_FORMERR:
                                    if qname_obj is not None and qname_obj.zone.server_responsive_valid_with_edns_flag(server,client,f):
                                        err = Errors.FormError(tcp=response.query.tcp_first, msg_size=response.msg_size, intermittent=True)
                                    else:
                                        err = Errors.ResponseErrorWithEDNSFlag(response_error=Errors.FormError(tcp=response.query.tcp_first, msg_size=response.msg_size, intermittent=False), flag=dns.flags.edns_to_text(f))
                                elif response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_TIMEOUT:
                                    if qname_obj is not None and qname_obj.zone.server_responsive_with_edns_flag(server,client,f):
                                        err = Errors.Timeout(tcp=response.query.tcp_first, attempts=response.responsive_cause_index+1, intermittent=True)
                                    else:
                                        err = Errors.ResponseErrorWithEDNSFlag(response_error=Errors.Timeout(tcp=response.query.tcp_first, attempts=response.responsive_cause_index+1, intermittent=False), flag=dns.flags.edns_to_text(f))
                                elif response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_OTHER:
                                    if qname_obj is not None and qname_obj.zone.server_responsive_valid_with_edns_flag(server,client,f):
                                        err = Errors.UnknownResponseError(tcp=response.query.tcp_first, rcode=dns.rcode.to_text(response.history[response.responsive_cause_index].cause_arg), intermittent=True)
                                    else:
                                        err = Errors.ResponseErrorWithEDNSFlag(response_error=Errors.UnknownResponseError(tcp=response.query.tcp_first, intermittent=False), flag=dns.flags.edns_to_text(f))
                                elif response.history[response.responsive_cause_index].cause == Q.RETRY_CAUSE_RCODE:
                                    if qname_obj is not None and qname_obj.zone.server_responsive_valid_with_edns_flag(server,client,f):
                                        err = Errors.InvalidRcode(tcp=response.query.tcp_first, rcode=dns.rcode.to_text(response.history[response.responsive_cause_index].cause_arg), intermittent=True)
                                    else:
                                        err = Errors.ResponseErrorWithEDNSFlag(response_error=Errors.InvalidRcode(tcp=response.query.tcp_first, rcode=dns.rcode.to_text(response.history[response.responsive_cause_index].cause_arg), intermittent=False), flag=dns.flags.edns_to_text(f))

                            #TODO handle this better
                            if err is None:
                                raise Exception('Unknown EDNS-flag-related error')

                        if err is not None:
                            break

            if err is not None:
                # warn on intermittent errors
                if isinstance(err, Errors.InvalidResponseError):
                    group = warnings
                # if the error really matters (e.g., due to DNSSEC), note an error
                elif qname_obj is not None and qname_obj.zone.signed:
                    group = errors
                # otherwise, warn
                else:
                    group = warnings

                Errors.DomainNameAnalysisError.insert_into_list(err, group, server, client, response)

        if not response.is_authoritative() and \
                not response.recursion_desired_and_available():
            Errors.DomainNameAnalysisError.insert_into_list(Errors.NotAuthoritative(), errors, server, client, response)

    def _populate_wildcard_status(self, query, rrset_info, qname_obj, supported_algs):
        for wildcard_name in rrset_info.wildcard_info:
            if qname_obj is None:
                zone_name = wildcard_info.parent()
            else:
                zone_name = qname_obj.zone.name

            servers_missing_nsec = set()
            for server, client in rrset_info.wildcard_info[wildcard_name].servers_clients:
                for response in rrset_info.wildcard_info[wildcard_name].servers_clients[(server,client)]:
                    servers_missing_nsec.add((server,client,response))

            statuses = []
            status_by_response = {}
            for nsec_set_info in rrset_info.wildcard_info[wildcard_name].nsec_set_info:
                if nsec_set_info.use_nsec3:
                    status = Status.NSEC3StatusWildcard(rrset_info.rrset.name, wildcard_name, rrset_info.rrset.rdtype, zone_name, nsec_set_info)
                else:
                    status = Status.NSECStatusWildcard(rrset_info.rrset.name, wildcard_name, rrset_info.rrset.rdtype, zone_name, nsec_set_info)

                for nsec_rrset_info in nsec_set_info.rrsets.values():
                    self._populate_rrsig_status(query, nsec_rrset_info, qname_obj, supported_algs)

                if status.validation_status == Status.NSEC_STATUS_VALID:
                    if status not in statuses:
                        statuses.append(status)

                for server, client in nsec_set_info.servers_clients:
                    for response in nsec_set_info.servers_clients[(server,client)]:
                        if (server,client,response) in servers_missing_nsec:
                            servers_missing_nsec.remove((server,client,response))
                        if status.validation_status == Status.NSEC_STATUS_VALID:
                            if (server,client,response) in status_by_response:
                                del status_by_response[(server,client,response)]
                        else:
                            status_by_response[(server,client,response)] = status

            for (server,client,response), status in status_by_response.items():
                if status not in statuses:
                    statuses.append(status)

            self.wildcard_status[rrset_info.wildcard_info[wildcard_name]] = statuses

            for server, client, response in servers_missing_nsec:
                # by definition, DNSSEC was requested (otherwise we
                # wouldn't know this was a wildcard), so no need to
                # check for DO bit in request
                Errors.DomainNameAnalysisError.insert_into_list(Errors.MissingNSECForWildcard(), self.rrset_errors[rrset_info], server, client, response)

    def _populate_rrsig_status(self, query, rrset_info, qname_obj, supported_algs, populate_response_errors=True):
        self.rrset_warnings[rrset_info] = []
        self.rrset_errors[rrset_info] = []
        self.rrsig_status[rrset_info] = {}

        if qname_obj is None:
            zone_name = None
        else:
            zone_name = qname_obj.zone.name

        if qname_obj is None:
            dnssec_algorithms_in_dnskey = set()
            dnssec_algorithms_in_ds = set()
            dnssec_algorithms_in_dlv = set()
        else:
            dnssec_algorithms_in_dnskey = qname_obj.zone.dnssec_algorithms_in_dnskey
            if query.rdtype == dns.rdatatype.DLV:
                dnssec_algorithms_in_ds = set()
                dnssec_algorithms_in_dlv = set()
            else:
                dnssec_algorithms_in_ds = qname_obj.zone.dnssec_algorithms_in_ds
                dnssec_algorithms_in_dlv = qname_obj.zone.dnssec_algorithms_in_dlv

        # handle DNAMEs
        has_dname = set()
        if rrset_info.rrset.rdtype == dns.rdatatype.CNAME:
            if rrset_info.dname_info is not None:
                dname_info_list = [rrset_info.dname_info]
                dname_status = Status.CNAMEFromDNAMEStatus(rrset_info, None)
            elif rrset_info.cname_info_from_dname:
                dname_info_list = [c.dname_info for c in rrset_info.cname_info_from_dname]
                dname_status = Status.CNAMEFromDNAMEStatus(rrset_info.cname_info_from_dname[0], rrset_info)
            else:
                dname_info_list = []
                dname_status = None

            if dname_info_list:
                for dname_info in dname_info_list:
                    for server, client in dname_info.servers_clients:
                        has_dname.update([(server,client,response) for response in dname_info.servers_clients[(server,client)]])

                if rrset_info.rrset.name not in self.dname_status:
                    self.dname_status[rrset_info] = []
                self.dname_status[rrset_info].append(dname_status)

        algs_signing_rrset = {}
        if dnssec_algorithms_in_dnskey or dnssec_algorithms_in_ds or dnssec_algorithms_in_dlv:
            for server, client in rrset_info.servers_clients:
                for response in rrset_info.servers_clients[(server, client)]:
                    if (server, client, response) not in has_dname:
                        algs_signing_rrset[(server, client, response)] = set()

        for rrsig in rrset_info.rrsig_info:
            self.rrsig_status[rrset_info][rrsig] = {}

            signer = self.get_name(rrsig.signer)

            #XXX
            if signer is not None:

                if signer.stub:
                    continue

                for server, client in rrset_info.rrsig_info[rrsig].servers_clients:
                    for response in rrset_info.rrsig_info[rrsig].servers_clients[(server,client)]:
                        if (server,client,response) not in algs_signing_rrset:
                            continue
                        algs_signing_rrset[(server,client,response)].add(rrsig.algorithm)
                        if not dnssec_algorithms_in_dnskey.difference(algs_signing_rrset[(server,client,response)]) and \
                                not dnssec_algorithms_in_ds.difference(algs_signing_rrset[(server,client,response)]) and \
                                not dnssec_algorithms_in_dlv.difference(algs_signing_rrset[(server,client,response)]):
                            del algs_signing_rrset[(server,client,response)]

                # define self-signature
                self_sig = rrset_info.rrset.rdtype == dns.rdatatype.DNSKEY and rrsig.signer == rrset_info.rrset.name

                checked_keys = set()
                for dnskey_set, dnskey_meta in signer.get_dnskey_sets():
                    validation_status_mapping = { True: set(), False: set(), None: set() }
                    for dnskey in dnskey_set:
                        # if we've already checked this key (i.e., in
                        # another DNSKEY RRset) then continue
                        if dnskey in checked_keys:
                            continue
                        # if this is a RRSIG over DNSKEY RRset, then make sure we're validating
                        # with a DNSKEY that is actually in the set
                        if self_sig and dnskey.rdata not in rrset_info.rrset:
                            continue
                        checked_keys.add(dnskey)
                        if not (dnskey.rdata.protocol == 3 and \
                                rrsig.key_tag in (dnskey.key_tag, dnskey.key_tag_no_revoke) and \
                                rrsig.algorithm == dnskey.rdata.algorithm):
                            continue
                        rrsig_status = Status.RRSIGStatus(rrset_info, rrsig, dnskey, zone_name, fmt.datetime_to_timestamp(self.analysis_end), algorithm_unknown=rrsig.algorithm not in supported_algs)
                        validation_status_mapping[rrsig_status.signature_valid].add(rrsig_status)

                    # if we got results for multiple keys, then just select the one that validates
                    for status in True, False, None:
                        if validation_status_mapping[status]:
                            for rrsig_status in validation_status_mapping[status]:
                                self.rrsig_status[rrsig_status.rrset][rrsig_status.rrsig][rrsig_status.dnskey] = rrsig_status

                                if self.is_zone() and rrset_info.rrset.name == self.name and \
                                        rrset_info.rrset.rdtype != dns.rdatatype.DS and \
                                        rrsig_status.dnskey is not None:
                                    if rrset_info.rrset.rdtype == dns.rdatatype.DNSKEY:
                                        self.ksks.add(rrsig_status.dnskey)
                                    else:
                                        self.zsks.add(rrsig_status.dnskey)

                                key = rrsig_status.rrset, rrsig_status.rrsig
                            break

            # no corresponding DNSKEY
            if not self.rrsig_status[rrset_info][rrsig]:
                rrsig_status = Status.RRSIGStatus(rrset_info, rrsig, None, self.zone.name, fmt.datetime_to_timestamp(self.analysis_end), algorithm_unknown=rrsig.algorithm not in supported_algs)
                self.rrsig_status[rrsig_status.rrset][rrsig_status.rrsig][None] = rrsig_status

        # list errors for rrsets with which no RRSIGs were returned or not all algorithms were accounted for
        for server,client,response in algs_signing_rrset:
            errors = self.rrset_errors[rrset_info]
            # report an error if all RRSIGs are missing
            if not algs_signing_rrset[(server,client,response)]:
                if response.dnssec_requested():
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.MissingRRSIG(), errors, server, client, response)
                elif qname_obj is not None and qname_obj.zone.server_responsive_with_do(server,client):
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.UnableToRetrieveDNSSECRecords(), errors, server, client, response)
            else:
                # report an error if RRSIGs for one or more algorithms are missing
                for alg in dnssec_algorithms_in_dnskey.difference(algs_signing_rrset[(server,client,response)]):
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.MissingRRSIGForAlgDNSKEY(algorithm=alg), errors, server, client, response)
                for alg in dnssec_algorithms_in_ds.difference(algs_signing_rrset[(server,client,response)]):
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.MissingRRSIGForAlgDS(algorithm=alg), errors, server, client, response)
                for alg in dnssec_algorithms_in_dlv.difference(algs_signing_rrset[(server,client,response)]):
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.MissingRRSIGForAlgDLV(algorithm=alg), errors, server, client, response)

        self._populate_wildcard_status(query, rrset_info, qname_obj, supported_algs)

        if populate_response_errors:
            for server,client in rrset_info.servers_clients:
                for response in rrset_info.servers_clients[(server,client)]:
                    self._populate_response_errors(qname_obj, response, server, client, self.rrset_warnings[rrset_info], self.rrset_errors[rrset_info])

    def _populate_invalid_response_status(self, query):
        self.response_errors[query] = []
        for error_info in query.error_info:
            for server, client in error_info.servers_clients:
                for response in error_info.servers_clients[(server, client)]:
                    if error_info.code == Q.RESPONSE_ERROR_NETWORK_ERROR:
                        Errors.DomainNameAnalysisError.insert_into_list(Errors.NetworkError(tcp=response.effective_tcp, intermittent=False, errno=errno.errorcode.get(error_info.arg, 'UNKNOWN')), self.response_errors[query], server, client, response)
                    if error_info.code == Q.RESPONSE_ERROR_FORMERR:
                        Errors.DomainNameAnalysisError.insert_into_list(Errors.FormError(tcp=response.effective_tcp, intermittent=False, msg_size=response.msg_size), self.response_errors[query], server, client, response)
                    if error_info.code == Q.RESPONSE_ERROR_TIMEOUT:
                        attempts = 1
                        for i in range(len(response.history) - 1, -1, -1):
                            if response.history[i].action in (Q.RETRY_ACTION_USE_TCP, Q.RETRY_ACTION_USE_UDP):
                                break
                            attempts += 1
                        Errors.DomainNameAnalysisError.insert_into_list(Errors.Timeout(tcp=response.effective_tcp, intermittent=False, attempts=attempts), self.response_errors[query], server, client, response)
                    if error_info.code == Q.RESPONSE_ERROR_OTHER:
                        Errors.DomainNameAnalysisError.insert_into_list(Errors.UnknownResponseError(tcp=response.effective_tcp, intermittent=False), self.response_errors[query], server, client, response)
                    if error_info.code == Q.RESPONSE_ERROR_INVALID_RCODE:
                        Errors.DomainNameAnalysisError.insert_into_list(Errors.InvalidRcode(tcp=response.effective_tcp, intermittent=False, rcode=dns.rcode.to_text(response.message.rcode())), self.response_errors[query], server, client, response)

    def _populate_rrsig_status_all(self, supported_algs, level):
        self.rrset_warnings = {}
        self.rrset_errors = {}
        self.rrsig_status = {}
        self.dname_status = {}
        self.wildcard_status = {}
        self.response_errors = {}

        if self.is_zone():
            self.zsks = set()
            self.ksks = set()

        _logger.debug('Assessing RRSIG status of %s...' % (fmt.humanize_name(self.name)))
        required_rdtypes = self._rdtypes_for_analysis_level(level)
        for (qname, rdtype), query in self.queries.items():

            if level > self.RDTYPES_ALL and qname not in (self.name, self.dlv_name):
                continue

            if required_rdtypes is not None and rdtype not in required_rdtypes:
                continue

            items_to_validate = []
            for rrset_info in query.answer_info:
                items_to_validate.append(rrset_info)
                if rrset_info.dname_info is not None:
                    items_to_validate.append(rrset_info.dname_info)
                for cname_rrset_info in rrset_info.cname_info_from_dname:
                    items_to_validate.append(cname_rrset_info.dname_info)
                    items_to_validate.append(cname_rrset_info)

            for rrset_info in items_to_validate:
                qname_obj = self.get_name(rrset_info.rrset.name)
                if rdtype == dns.rdatatype.DS:
                    qname_obj = qname_obj.parent
                elif rdtype == dns.rdatatype.DLV:
                    qname_obj = qname_obj.dlv_parent

                self._populate_rrsig_status(query, rrset_info, qname_obj, supported_algs)

            self._populate_invalid_response_status(query)

    def _finalize_key_roles(self):
        if self.is_zone():
            self.published_keys = set(self.get_dnskeys()).difference(self.zsks.union(self.ksks))
            self.revoked_keys = set(filter(lambda x: x.rdata.flags & fmt.DNSKEY_FLAGS['revoke'], self.get_dnskeys()))

    def _populate_ns_status(self, warn_no_ipv4=True, warn_no_ipv6=False):
        if not self.is_zone():
            return

        if self.parent is None:
            return

        assert warn_no_ipv4 or warn_no_ipv6, 'At least one of warn_no_ipv4 and warn_no_ipv6 must be True when calling _populate_ns_status()'

        all_names = self.get_ns_names()
        names_from_child = self.get_ns_names_in_child()
        names_from_parent = self.get_ns_names_in_parent()

        auth_ns_response = self.queries[(self.name, dns.rdatatype.NS)].is_valid_complete_authoritative_response_any()

        ips_from_child = self.get_servers_in_child()
        ips_from_parent = self.get_servers_in_parent()

        glue_mapping = self.get_glue_ip_mapping()
        auth_mapping = self.get_auth_ns_ip_mapping()

        ns_names_not_in_child = []
        ns_names_not_in_parent = []
        names_error_resolving = []
        names_with_wrong_glue_v4 = []
        names_with_wrong_glue_v6 = []
        names_missing_glue_v4_warn = []
        names_missing_glue_v6_warn = []
        names_missing_glue_v4_err = []
        names_missing_glue_v6_err = []
        names_missing_auth_v4 = []
        names_missing_auth_v6 = []

        for name in all_names:
            if name in auth_mapping:
                ip4_auth_addrs = set(filter(lambda x: x.version == 4, auth_mapping[name]))
                ip6_auth_addrs = set(filter(lambda x: x.version == 6, auth_mapping[name]))
                if not ip4_auth_addrs and warn_no_ipv4:
                    names_missing_auth_v4.append(name)
                if not ip6_auth_addrs and warn_no_ipv6:
                    names_missing_auth_v6.append(name)
            else:
                ip4_auth_addrs = set()
                ip6_auth_addrs = set()
                names_error_resolving.append(name)

            if names_from_parent:
                name_in_parent = name in names_from_parent
            elif self.delegation_status == Status.DELEGATION_STATUS_INCOMPLETE:
                name_in_parent = False
            else:
                name_in_parent = None

            if name_in_parent:
                ip4_glue_addrs = set(filter(lambda x: x.version == 4, glue_mapping[name]))
                ip6_glue_addrs = set(filter(lambda x: x.version == 6, glue_mapping[name]))

                # if glue is required and not supplied
                if name.is_subdomain(self.name):
                    if not ip4_glue_addrs:
                        # if we warn on no IPv4 glue or if there is no IPv6
                        # glue, then make it an error
                        if warn_no_ipv4 or not ip6_glue_addrs:
                            names_missing_glue_v4_err.append(name)
                        # otherwise, if there are authoritative IPv4 addresses,
                        # then make it a warning
                        elif ip4_auth_addrs:
                            names_missing_glue_v4_warn.append(name)
                    if not ip6_glue_addrs:
                        # if we warn on no IPv6 glue or if there is no IPv4
                        # glue, then make it an error
                        if warn_no_ipv6 or not ip4_glue_addrs:
                            names_missing_glue_v6_err.append(name)
                        # otherwise, if there are authoritative IPv6 addresses,
                        # then make it a warning
                        elif ip6_auth_addrs:
                            names_missing_glue_v6_warn.append(name)

                # if glue is supplied, check that it is correct
                if ip4_glue_addrs and ip4_auth_addrs and ip4_glue_addrs != ip4_auth_addrs:
                    names_with_wrong_glue_v4.append((name,ip4_glue_addrs,ip4_auth_addrs))
                if ip6_glue_addrs and ip6_auth_addrs and ip6_glue_addrs != ip6_auth_addrs:
                    names_with_wrong_glue_v6.append((name,ip6_glue_addrs,ip6_auth_addrs))

            elif name_in_parent is False:
                ns_names_not_in_parent.append(name)

            if name not in names_from_child and auth_ns_response:
                ns_names_not_in_child.append(name)

        if ns_names_not_in_child:
            ns_names_not_in_child.sort()
            self.delegation_warnings[dns.rdatatype.DS].append(Errors.NSNameNotInChild(names=map(lambda x: x.canonicalize().to_text(), ns_names_not_in_child), parent=self.parent_name().canonicalize().to_text()))

        if ns_names_not_in_parent:
            ns_names_not_in_child.sort()
            self.delegation_warnings[dns.rdatatype.DS].append(Errors.NSNameNotInParent(names=map(lambda x: x.canonicalize().to_text(), ns_names_not_in_parent), parent=self.parent_name().canonicalize().to_text()))

        if names_error_resolving:
            names_error_resolving.sort()
            self.delegation_errors[dns.rdatatype.DS].append(Errors.ErrorResolvingNSName(names=map(lambda x: x.canonicalize().to_text(), names_error_resolving)))

        if names_with_wrong_glue_v4:
            names_with_wrong_glue_v4.sort()
            for name, ip4_glue_addrs, ip4_auth_addrs in names_with_wrong_glue_v4:
                ip4_glue_addrs = list(ip4_glue_addrs)
                ip4_glue_addrs.sort()
                ip4_auth_addrs = list(ip4_auth_addrs)
                ip4_auth_addrs.sort()
                self.delegation_warnings[dns.rdatatype.DS].append(Errors.GlueMismatchErrorIPv4(name=name.canonicalize().to_text(), glue_addresses=ip4_glue_addrs, auth_addresses=ip4_auth_addrs))

        if names_with_wrong_glue_v6:
            names_with_wrong_glue_v6.sort()
            for name, ip6_glue_addrs, ip6_auth_addrs in names_with_wrong_glue_v6:
                ip6_glue_addrs = list(ip6_glue_addrs)
                ip6_glue_addrs.sort()
                ip6_auth_addrs = list(ip6_auth_addrs)
                ip6_auth_addrs.sort()
                self.delegation_warnings[dns.rdatatype.DS].append(Errors.GlueMismatchErrorIPv6(name=name.canonicalize().to_text(), glue_addresses=ip6_glue_addrs, auth_addresses=ip6_auth_addrs))

        if names_missing_glue_v4_warn:
            names_missing_glue_v4_warn.sort()
            self.delegation_warnings[dns.rdatatype.DS].append(Errors.MissingIPv4GlueForNSName(names=map(lambda x: x.canonicalize().to_text(), names_missing_glue_v4_warn)))

        if names_missing_glue_v6_warn:
            names_missing_glue_v6_warn.sort()
            self.delegation_warnings[dns.rdatatype.DS].append(Errors.MissingIPv6GlueForNSName(names=map(lambda x: x.canonicalize().to_text(), names_missing_glue_v6_warn)))

        if names_missing_glue_v4_err:
            names_missing_glue_v4_err.sort()
            self.delegation_errors[dns.rdatatype.DS].append(Errors.MissingIPv4GlueForNSName(names=map(lambda x: x.canonicalize().to_text(), names_missing_glue_v4_err)))

        if names_missing_glue_v6_err:
            names_missing_glue_v6_err.sort()
            self.delegation_errors[dns.rdatatype.DS].append(Errors.MissingIPv6GlueForNSName(names=map(lambda x: x.canonicalize().to_text(), names_missing_glue_v6_err)))

        if names_missing_auth_v4:
            names_missing_auth_v4.sort()
            self.delegation_errors[dns.rdatatype.DS].append(Errors.NoIPv4AddressForNSName(names=map(lambda x: x.canonicalize().to_text(), names_missing_auth_v4)))

        if names_missing_auth_v6:
            names_missing_auth_v6.sort()
            self.delegation_errors[dns.rdatatype.DS].append(Errors.NoIPv6AddressForNSName(names=map(lambda x: x.canonicalize().to_text(), names_missing_auth_v6)))

    def _populate_delegation_status(self, supported_algs, supported_digest_algs):
        self.ds_status_by_ds = {}
        self.ds_status_by_dnskey = {}
        self.delegation_errors = {}
        self.delegation_warnings = {}
        self.delegation_status = {}

        self._populate_ds_status(dns.rdatatype.DS, supported_algs, supported_digest_algs)
        if self.dlv_parent is not None:
            self._populate_ds_status(dns.rdatatype.DLV, supported_algs, supported_digest_algs)
        self._populate_ns_status()
        self._populate_server_status()

    def _populate_ds_status(self, rdtype, supported_algs, supported_digest_algs):
        if rdtype not in (dns.rdatatype.DS, dns.rdatatype.DLV):
            raise ValueError('Type can only be DS or DLV.')
        if self.parent is None:
            return
        if rdtype == dns.rdatatype.DLV:
            name = self.dlv_name
            if name is None:
                raise ValueError('No DLV specified for DomainNameAnalysis object.')
        else:
            name = self.name

        _logger.debug('Assessing delegation status of %s...' % (fmt.humanize_name(self.name)))
        self.ds_status_by_ds[rdtype] = {}
        self.ds_status_by_dnskey[rdtype] = {}
        self.delegation_warnings[rdtype] = []
        self.delegation_errors[rdtype] = []
        self.delegation_status[rdtype] = None

        try:
            ds_rrset_answer_info = self.queries[(name, rdtype)].answer_info
        except KeyError:
            # zones should have DS queries
            if self.is_zone():
                raise
            else:
                return

        secure_path = False

        bailiwick_map, default_bailiwick = self.get_bailiwick_mapping()

        if (self.name, dns.rdatatype.DNSKEY) in self.queries:
            dnskey_multiquery = self.queries[(self.name, dns.rdatatype.DNSKEY)]
        else:
            dnskey_multiquery = self.QUERY_CLASS(self.name, dns.rdatatype.DNSKEY, dns.rdataclass.IN)

        # populate all the servers queried for DNSKEYs to determine
        # what problems there were with regard to DS records and if
        # there is at least one match
        dnskey_server_client_responses = set()
        for dnskey_query in dnskey_multiquery.queries.values():
            for server in dnskey_query.responses:
                bailiwick = bailiwick_map.get(server, default_bailiwick)
                for client in dnskey_query.responses[server]:
                    response = dnskey_query.responses[server][client]
                    if response.is_valid_response() and response.is_complete_response() and not response.is_referral(self.name, dns.rdatatype.DNSKEY, bailiwick):
                        dnskey_server_client_responses.add((server,client,response))

        for ds_rrset_info in ds_rrset_answer_info:
            # there are CNAMEs that show up here...
            if not (ds_rrset_info.rrset.name == name and ds_rrset_info.rrset.rdtype == rdtype):
                continue

            # for each set of DS records provided by one or more servers,
            # identify the set of DNSSEC algorithms and the set of digest
            # algorithms per algorithm/key tag combination
            ds_algs = set()
            supported_ds_algs = set()
            digest_algs = {}
            for ds_rdata in ds_rrset_info.rrset:
                if (ds_rdata.algorithm, ds_rdata.key_tag) not in digest_algs:
                    digest_algs[(ds_rdata.algorithm, ds_rdata.key_tag)] = set()
                digest_algs[(ds_rdata.algorithm, ds_rdata.key_tag)].add(ds_rdata.digest_type)
                if ds_rdata.algorithm in supported_algs and ds_rdata.digest_type in supported_digest_algs:
                    supported_ds_algs.add(ds_rdata.algorithm)
                ds_algs.add(ds_rdata.algorithm)

            if supported_ds_algs:
                secure_path = True

            algs_signing_sep = {}
            algs_validating_sep = {}
            for server,client,response in dnskey_server_client_responses:
                algs_signing_sep[(server,client,response)] = set()
                algs_validating_sep[(server,client,response)] = set()

            for ds_rdata in ds_rrset_info.rrset:
                self.ds_status_by_ds[rdtype][ds_rdata] = {}

                for dnskey_info in dnskey_multiquery.answer_info:
                    # there are CNAMEs that show up here...
                    if not (dnskey_info.rrset.name == self.name and dnskey_info.rrset.rdtype == dns.rdatatype.DNSKEY):
                        continue

                    validation_status_mapping = { True: set(), False: set(), None: set() }
                    for dnskey_rdata in dnskey_info.rrset:
                        dnskey = self._dnskeys[dnskey_rdata]

                        if dnskey not in self.ds_status_by_dnskey[rdtype]:
                            self.ds_status_by_dnskey[rdtype][dnskey] = {}

                        # if the key tag doesn't match, then go any farther
                        if not (ds_rdata.key_tag in (dnskey.key_tag, dnskey.key_tag_no_revoke) and \
                                ds_rdata.algorithm == dnskey.rdata.algorithm):
                            continue

                        # check if the digest is a match
                        ds_status = Status.DSStatus(ds_rdata, ds_rrset_info, dnskey, digest_algorithm_unknown=ds_rdata.digest_type not in supported_digest_algs)
                        validation_status_mapping[ds_status.digest_valid].add(ds_status)

                        # ignore DS algorithm 1 if algorithm 2 exists
                        ignore_ds_alg = (ds_rdata.digest_type == 1) and (2 in digest_algs[(ds_rdata.algorithm, ds_rdata.key_tag)]) and (2 in supported_digest_algs)

                        for rrsig in dnskey_info.rrsig_info:
                            # move along if DNSKEY is not self-signing
                            if dnskey not in self.rrsig_status[dnskey_info][rrsig]:
                                continue
                            
                            # move along if key tag is not the same (i.e., revoke)
                            if dnskey.key_tag != rrsig.key_tag:
                                continue

                            for (server,client) in dnskey_info.rrsig_info[rrsig].servers_clients:
                                for response in dnskey_info.rrsig_info[rrsig].servers_clients[(server,client)]:
                                    if (server,client,response) in algs_signing_sep:
                                        # note that this algorithm is part of a self-signing DNSKEY
                                        algs_signing_sep[(server,client,response)].add(rrsig.algorithm)
                                        if not ds_algs.difference(algs_signing_sep[(server,client,response)]):
                                            del algs_signing_sep[(server,client,response)]

                                    if (server,client,response) in algs_validating_sep:
                                        # retrieve the status of the DNSKEY RRSIG
                                        rrsig_status = self.rrsig_status[dnskey_info][rrsig][dnskey]

                                        # if the DS digest and the RRSIG are both valid, and the digest algorithm
                                        # is not deprecated then mark it as a SEP
                                        if ds_status.validation_status == Status.DS_STATUS_VALID and \
                                                rrsig_status.validation_status == Status.RRSIG_STATUS_VALID and \
                                                not ignore_ds_alg:
                                            # note that this algorithm is part of a successful self-signing DNSKEY
                                            algs_validating_sep[(server,client,response)].add(rrsig.algorithm)
                                            if not ds_algs.difference(algs_validating_sep[(server,client,response)]):
                                                del algs_validating_sep[(server,client,response)]

                    # if we got results for multiple keys, then just select the one that validates
                    for status in True, False, None:
                        if validation_status_mapping[status]:
                            for ds_status in validation_status_mapping[status]:
                                self.ds_status_by_ds[rdtype][ds_status.ds][ds_status.dnskey] = ds_status
                                self.ds_status_by_dnskey[rdtype][ds_status.dnskey][ds_status.ds] = ds_status
                            break

                # no corresponding DNSKEY
                if not self.ds_status_by_ds[rdtype][ds_rdata]:
                    ds_status = Status.DSStatus(ds_rdata, ds_rrset_info, None)
                    self.ds_status_by_ds[rdtype][ds_rdata][None] = ds_status
                    if None not in self.ds_status_by_dnskey[rdtype]:
                        self.ds_status_by_dnskey[rdtype][None] = {}
                    self.ds_status_by_dnskey[rdtype][None][ds_rdata] = ds_status

            if dnskey_server_client_responses:
                if not algs_validating_sep:
                    self.delegation_status[rdtype] = Status.DELEGATION_STATUS_SECURE
                else:
                    for server,client,response in dnskey_server_client_responses:
                        if (server,client,response) not in algs_validating_sep or \
                                supported_ds_algs.intersection(algs_validating_sep[(server,client,response)]):
                            self.delegation_status[rdtype] = Status.DELEGATION_STATUS_SECURE
                        elif supported_ds_algs:
                            Errors.DomainNameAnalysisError.insert_into_list(Errors.NoSEP(source=dns.rdatatype.to_text(rdtype)), self.delegation_errors[rdtype], server, client, response)

                # report an error if one or more algorithms are incorrectly validated
                for (server,client,response) in algs_signing_sep:
                    for alg in algs_signing_sep[(server,client,response)]:
                        Errors.DomainNameAnalysisError.insert_into_list(Errors.MissingSEPForAlg(algorithm=alg, source=dns.rdatatype.to_text(rdtype)), self.delegation_errors[rdtype], server, client, response)
            else:
                Errors.DomainNameAnalysisError.insert_into_list(Errors.NoSEP(source=dns.rdatatype.to_text(rdtype)), self.delegation_errors[rdtype], None, None, None)

        if self.delegation_status[rdtype] is None:
            if ds_rrset_answer_info:
                if secure_path:
                    self.delegation_status[rdtype] = Status.DELEGATION_STATUS_BOGUS
                else:
                    self.delegation_status[rdtype] = Status.DELEGATION_STATUS_INSECURE
            elif self.parent.signed:
                self.delegation_status[rdtype] = Status.DELEGATION_STATUS_BOGUS
                for nsec_status_list in [self.nxdomain_status[n] for n in self.nxdomain_status if n.qname == name and n.rdtype == dns.rdatatype.DS] + \
                        [self.nodata_status[n] for n in self.nodata_status if n.qname == name and n.rdtype == dns.rdatatype.DS]:
                    for nsec_status in nsec_status_list:
                        if nsec_status.validation_status == Status.NSEC_STATUS_VALID:
                            self.delegation_status[rdtype] = Status.DELEGATION_STATUS_INSECURE
                            break
            else:
                self.delegation_status[rdtype] = Status.DELEGATION_STATUS_INSECURE

        #XXX the remaining checks need consideration for recursive

        # if no servers (designated or stealth authoritative) respond or none
        # respond authoritatively, then make the delegation as lame
        if not self.get_auth_or_designated_servers():
            if self.delegation_status[rdtype] == Status.DELEGATION_STATUS_INSECURE:
                self.delegation_status[rdtype] = Status.DELEGATION_STATUS_LAME
        elif not self.get_responsive_auth_or_designated_servers():
            if self.delegation_status[rdtype] == Status.DELEGATION_STATUS_INSECURE:
                self.delegation_status[rdtype] = Status.DELEGATION_STATUS_LAME
        elif not self.get_valid_auth_or_designated_servers():
            if self.delegation_status[rdtype] == Status.DELEGATION_STATUS_INSECURE:
                self.delegation_status[rdtype] = Status.DELEGATION_STATUS_LAME
        elif not self._auth_servers_clients:
            if self.delegation_status[rdtype] == Status.DELEGATION_STATUS_INSECURE:
                self.delegation_status[rdtype] = Status.DELEGATION_STATUS_LAME

        if rdtype == dns.rdatatype.DS:
            try:
                ds_nxdomain_info = filter(lambda x: x.qname == name and x.rdtype == dns.rdatatype.DS, self.queries[(name, rdtype)].nxdomain_info)[0]
            except IndexError:
                pass
            else:
                err = Errors.NoNSInParent(parent=self.parent_name())
                err.servers_clients.update(ds_nxdomain_info.servers_clients)
                self.delegation_errors[rdtype].append(err)
                if self.delegation_status[rdtype] == Status.DELEGATION_STATUS_INSECURE:
                    self.delegation_status[rdtype] = Status.DELEGATION_STATUS_INCOMPLETE

    def _populate_server_status(self):
        if not self.is_zone():
            return

        if self.parent is None:
            return

        designated_servers = self.get_designated_servers()
        servers_queried_udp = set(filter(lambda x: x[0] in designated_servers, self._all_servers_clients_queried))
        servers_queried_tcp = set(filter(lambda x: x[0] in designated_servers, self._all_servers_clients_queried_tcp))
        servers_queried = servers_queried_udp.union(servers_queried_tcp)

        unresponsive_udp = servers_queried_udp.difference(self._responsive_servers_clients_udp)
        unresponsive_tcp = servers_queried_tcp.difference(self._responsive_servers_clients_tcp)
        invalid_response = servers_queried.intersection(self._responsive_servers_clients_udp).difference(self._valid_servers_clients)
        not_authoritative = servers_queried.intersection(self._valid_servers_clients).difference(self._auth_servers_clients)

        if unresponsive_udp:
            err = Errors.ServerUnresponsiveUDP()
            for server, client in unresponsive_udp:
                err.add_server_client(server, client, None)
            self.delegation_errors[dns.rdatatype.DS].append(err)

        if unresponsive_tcp:
            err = Errors.ServerUnresponsiveTCP()
            for server, client in unresponsive_tcp:
                err.add_server_client(server, client, None)
            self.delegation_errors[dns.rdatatype.DS].append(err)

        if invalid_response:
            err = Errors.ServerInvalidResponse()
            for server, client in invalid_response:
                err.add_server_client(server, client, None)
            self.delegation_errors[dns.rdatatype.DS].append(err)

        if not_authoritative:
            err = Errors.ServerNotAuthoritative()
            for server, client in not_authoritative:
                err.add_server_client(server, client, None)
            self.delegation_errors[dns.rdatatype.DS].append(err)

    def _populate_negative_response_status(self, query, neg_response_info, \
            bad_soa_error_cls, missing_soa_error_cls, upward_referral_error_cls, missing_nsec_error_cls, \
            nsec_status_cls, nsec3_status_cls, warnings, errors, supported_algs):

        qname_obj = self.get_name(neg_response_info.qname)
        if query.rdtype == dns.rdatatype.DS:
            qname_obj = qname_obj.parent

        soa_owner_name_for_servers = {}
        servers_without_soa = set()
        servers_missing_nsec = set()
        for server, client in neg_response_info.servers_clients:
            for response in neg_response_info.servers_clients[(server, client)]:
                servers_without_soa.add((server, client, response))
                servers_missing_nsec.add((server, client, response))

                self._populate_response_errors(qname_obj, response, server, client, warnings, errors)

        for soa_rrset_info in neg_response_info.soa_rrset_info:
            soa_owner_name = soa_rrset_info.rrset.name

            for server, client in soa_rrset_info.servers_clients:
                for response in soa_rrset_info.servers_clients[(server, client)]:
                    servers_without_soa.remove((server, client, response))
                    soa_owner_name_for_servers[(server,client,response)] = soa_owner_name

            if soa_owner_name != qname_obj.zone.name:
                err = Errors.DomainNameAnalysisError.insert_into_list(bad_soa_error_cls(soa_owner_name=soa_owner_name.canonicalize().to_text(), zone_name=qname_obj.zone.name.canonicalize().to_text()), errors, None, None, None)
                if neg_response_info.qname == query.qname:
                    err.servers_clients.update(soa_rrset_info.servers_clients)
                else:
                    for server,client in soa_rrset_info.servers_clients:
                        for response in soa_rrset_info.servers_clients[(server,client)]:
                            if response.recursion_desired_and_available():
                                err.add_server_client(server, client, response)

            self._populate_rrsig_status(query, soa_rrset_info, self.get_name(soa_owner_name), supported_algs, populate_response_errors=False)

        for server,client,response in servers_without_soa:
            if neg_response_info.qname == query.qname or response.recursion_desired_and_available():
                # check for an upward referral
                if upward_referral_error_cls is not None and response.is_upward_referral(qname_obj.zone.name):
                    Errors.DomainNameAnalysisError.insert_into_list(upward_referral_error_cls(), errors, server, client, response)
                else:
                    Errors.DomainNameAnalysisError.insert_into_list(missing_soa_error_cls(), errors, server, client, response)

        if upward_referral_error_cls is not None:
            try:
                index = errors.index(upward_referral_error_cls())
            except ValueError:
                pass
            else:
                upward_referral_error = errors[index]
                for notices in errors, warnings:
                    not_auth_notices = filter(lambda x: isinstance(x, Errors.NotAuthoritative), notices)
                    for notice in not_auth_notices:
                        for server, client in upward_referral_error.servers_clients:
                            for response in upward_referral_error.servers_clients[(server, client)]:
                                notice.remove_server_client(server, client, response)
                        if not notice.servers_clients:
                            notices.remove(notice)
                
        statuses = []
        status_by_response = {}
        for nsec_set_info in neg_response_info.nsec_set_info:
            if nsec_set_info.use_nsec3:
                status = nsec3_status_cls(neg_response_info.qname, query.rdtype, \
                        soa_owner_name_for_servers.get((server,client,response), qname_obj.zone.name), nsec_set_info)
            else:
                status = nsec_status_cls(neg_response_info.qname, query.rdtype, \
                        soa_owner_name_for_servers.get((server,client,response), qname_obj.zone.name), nsec_set_info)

            for nsec_rrset_info in nsec_set_info.rrsets.values():
                self._populate_rrsig_status(query, nsec_rrset_info, qname_obj, supported_algs, populate_response_errors=False)

            if status.validation_status == Status.NSEC_STATUS_VALID:
                if status not in statuses:
                    statuses.append(status)

            for server, client in nsec_set_info.servers_clients:
                for response in nsec_set_info.servers_clients[(server,client)]:
                    if (server,client,response) in servers_missing_nsec:
                        servers_missing_nsec.remove((server,client,response))
                    if status.validation_status == Status.NSEC_STATUS_VALID:
                        if (server,client,response) in status_by_response:
                            del status_by_response[(server,client,response)]
                    elif neg_response_info.qname == query.qname or response.recursion_desired_and_available():
                        status_by_response[(server,client,response)] = status

        for (server,client,response), status in status_by_response.items():
            if status not in statuses:
                statuses.append(status)

        for server, client, response in servers_missing_nsec:
            # report that no NSEC(3) records were returned
            if qname_obj.zone.signed and (neg_response_info.qname == query.qname or response.recursion_desired_and_available()):
                if response.dnssec_requested():
                    Errors.DomainNameAnalysisError.insert_into_list(missing_nsec_error_cls(), errors, server, client, response)
                elif qname_obj is not None and qname_obj.zone.server_responsive_with_do(server,client):
                    Errors.DomainNameAnalysisError.insert_into_list(Errors.UnableToRetrieveDNSSECRecords(), errors, server, client, response)

        return statuses

    def _populate_nxdomain_status(self, supported_algs, level):
        self.nxdomain_status = {}
        self.nxdomain_warnings = {}
        self.nxdomain_errors = {}

        _logger.debug('Assessing NXDOMAIN response status of %s...' % (fmt.humanize_name(self.name)))
        required_rdtypes = self._rdtypes_for_analysis_level(level)
        for (qname, rdtype), query in self.queries.items():
            if level > self.RDTYPES_ALL and qname not in (self.name, self.dlv_name):
                continue

            if required_rdtypes is not None and rdtype not in required_rdtypes:
                continue

            for neg_response_info in query.nxdomain_info:
                self.nxdomain_warnings[neg_response_info] = []
                self.nxdomain_errors[neg_response_info] = []
                self.nxdomain_status[neg_response_info] = \
                        self._populate_negative_response_status(query, neg_response_info, \
                                Errors.SOAOwnerNotZoneForNXDOMAIN, Errors.MissingSOAForNXDOMAIN, None, \
                                Errors.MissingNSECForNXDOMAIN, Status.NSECStatusNXDOMAIN, Status.NSEC3StatusNXDOMAIN, \
                                self.nxdomain_warnings[neg_response_info], self.nxdomain_errors[neg_response_info], \
                                supported_algs)

                # check for NOERROR/NXDOMAIN inconsistencies
                if neg_response_info.qname in self.yxdomain and rdtype not in (dns.rdatatype.DS, dns.rdatatype.DLV):
                    for (qname2, rdtype2), query2 in self.queries.items():
                        if rdtype2 in (dns.rdatatype.DS, dns.rdatatype.DLV):
                            continue

                        if required_rdtypes is not None and rdtype2 not in required_rdtypes:
                            continue

                        for rrset_info in filter(lambda x: x.rrset.name == neg_response_info.qname, query2.answer_info):
                            shared_servers_clients = set(rrset_info.servers_clients).intersection(neg_response_info.servers_clients)
                            if shared_servers_clients:
                                err1 = Errors.DomainNameAnalysisError.insert_into_list(Errors.InconsistentNXDOMAIN(qname=neg_response_info.qname, rdtype_nxdomain=dns.rdatatype.to_text(rdtype), rdtype_noerror=dns.rdatatype.to_text(query2.rdtype)), self.nxdomain_warnings[neg_response_info], None, None, None) 
                                err2 = Errors.DomainNameAnalysisError.insert_into_list(Errors.InconsistentNXDOMAIN(qname=neg_response_info.qname, rdtype_nxdomain=dns.rdatatype.to_text(rdtype), rdtype_noerror=dns.rdatatype.to_text(query2.rdtype)), self.rrset_warnings[rrset_info], None, None, None) 
                                for server, client in shared_servers_clients:
                                    for response in neg_response_info.servers_clients[(server, client)]:
                                        err1.add_server_client(server, client, response)
                                        err2.add_server_client(server, client, response)

                        for neg_response_info2 in filter(lambda x: x.qname == neg_response_info.qname, query2.nodata_info):
                            shared_servers_clients = set(neg_response_info2.servers_clients).intersection(neg_response_info.servers_clients)
                            if shared_servers_clients:
                                err1 = Errors.DomainNameAnalysisError.insert_into_list(Errors.InconsistentNXDOMAIN(qname=neg_response_info.qname, rdtype_nxdomain=dns.rdatatype.to_text(rdtype), rdtype_noerror=dns.rdatatype.to_text(query2.rdtype)), self.nxdomain_warnings[neg_response_info], None, None, None) 
                                err2 = Errors.DomainNameAnalysisError.insert_into_list(Errors.InconsistentNXDOMAIN(qname=neg_response_info.qname, rdtype_nxdomain=dns.rdatatype.to_text(rdtype), rdtype_noerror=dns.rdatatype.to_text(query2.rdtype)), self.nodata_warnings[neg_response_info2], None, None, None) 
                                for server, client in shared_servers_clients:
                                    for response in neg_response_info.servers_clients[(server, client)]:
                                        err1.add_server_client(server, client, response)
                                        err2.add_server_client(server, client, response)

    def _populate_nodata_status(self, supported_algs, level):
        self.nodata_status = {}
        self.nodata_warnings = {}
        self.nodata_errors = {}

        _logger.debug('Assessing NODATA response status of %s...' % (fmt.humanize_name(self.name)))
        required_rdtypes = self._rdtypes_for_analysis_level(level)
        for (qname, rdtype), query in self.queries.items():
            if level > self.RDTYPES_ALL and qname not in (self.name, self.dlv_name):
                continue

            if required_rdtypes is not None and rdtype not in required_rdtypes:
                continue

            for neg_response_info in query.nodata_info:
                self.nodata_warnings[neg_response_info] = []
                self.nodata_errors[neg_response_info] = []
                self.nodata_status[neg_response_info] = \
                        self._populate_negative_response_status(query, neg_response_info, \
                                Errors.SOAOwnerNotZoneForNODATA, Errors.MissingSOAForNODATA, Errors.UpwardReferral, \
                                Errors.MissingNSECForNODATA, Status.NSECStatusNoAnswer, Status.NSEC3StatusNoAnswer, \
                                self.nodata_warnings[neg_response_info], self.nodata_errors[neg_response_info], \
                                supported_algs)

    def _populate_dnskey_status(self, trusted_keys):
        if (self.name, dns.rdatatype.DNSKEY) not in self.queries:
            return

        trusted_keys_rdata = set([k for z, k in trusted_keys if z == self.name])
        trusted_keys_existing = set()
        trusted_keys_not_self_signing = set()

        # buid a list of responsive servers
        bailiwick_map, default_bailiwick = self.get_bailiwick_mapping()
        servers_responsive = set()
        for query in self.queries[(self.name, dns.rdatatype.DNSKEY)].queries.values():
            servers_responsive.update([(server,client,query.responses[server][client]) for (server,client) in query.servers_with_valid_complete_response(bailiwick_map, default_bailiwick)])

        # any errors point to their own servers_clients value
        for dnskey in self.get_dnskeys():
            if dnskey.rdata in trusted_keys_rdata:
                trusted_keys_existing.add(dnskey)
                if dnskey not in self.ksks:
                    trusted_keys_not_self_signing.add(dnskey)
            if dnskey in self.revoked_keys and dnskey not in self.ksks:
                err = Errors.RevokedNotSigning()
                err.servers_clients = dnskey.servers_clients
                dnskey.errors.append(err)
            if not self.is_zone():
                err = Errors.DNSKEYNotAtZoneApex()
                err.servers_clients = dnskey.servers_clients
                dnskey.errors.append(err)

            # if there were servers responsive for the query but that didn't return the dnskey
            servers_with_dnskey = set()
            for (server,client) in dnskey.servers_clients:
                for response in dnskey.servers_clients[(server,client)]:
                    servers_with_dnskey.add((server,client,response))
            servers_clients_without = servers_responsive.difference(servers_with_dnskey)
            if servers_clients_without:
                err = Errors.DNSKEYMissingFromServers()
                dnskey.errors.append(err)
                for (server,client,response) in servers_clients_without:
                    err.add_server_client(server, client, response)

        if not trusted_keys_existing.difference(trusted_keys_not_self_signing):
            for dnskey in trusted_keys_not_self_signing:
                err = Errors.TrustAnchorNotSigning()
                err.servers_clients = dnskey.servers_clients
                dnskey.errors.append(err)

    def _serialize_rrset_info(self, rrset_info, consolidate_clients=False, show_servers=True, loglevel=logging.DEBUG, html_format=False):
        d = collections.OrderedDict()

        if loglevel <= logging.INFO or (self.rrset_warnings[rrset_info] and loglevel <= logging.WARNING) or (self.rrset_errors[rrset_info] and loglevel <= logging.ERROR):
            d['description'] = unicode(rrset_info)

        if loglevel <= logging.DEBUG:
            d['rrset'] = rrset_info.serialize(include_rrsig_info=False, show_servers=show_servers, consolidate_clients=consolidate_clients, html_format=html_format)

        if self.rrsig_status[rrset_info]:
            d['rrsig'] = []
            rrsigs = self.rrsig_status[rrset_info].keys()
            rrsigs.sort()
            for rrsig in rrsigs:
                dnskeys = self.rrsig_status[rrset_info][rrsig].keys()
                dnskeys.sort()
                for dnskey in dnskeys:
                    rrsig_status = self.rrsig_status[rrset_info][rrsig][dnskey]
                    rrsig_serialized = rrsig_status.serialize(consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                    if rrsig_serialized:
                        d['rrsig'].append(rrsig_serialized)
            if not d['rrsig']:
                del d['rrsig']

        if rrset_info in self.dname_status:
            d['dname'] = []
            for dname_status in self.dname_status[rrset_info]:
                dname_serialized = dname_status.serialize(self._serialize_rrset_info, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                if dname_serialized:
                    d['dname'].append(dname_serialized)
            if not d['dname']:
                del d['dname']

        if rrset_info.wildcard_info:
            d['wildcard_proof'] = collections.OrderedDict()
            wildcard_names = rrset_info.wildcard_info.keys()
            wildcard_names.sort()
            for wildcard_name in wildcard_names:
                wildcard_name_str = wildcard_name.canonicalize().to_text()
                d['wildcard_proof'][wildcard_name_str] = []
                for nsec_status in self.wildcard_status[rrset_info.wildcard_info[wildcard_name]]:
                    nsec_serialized = nsec_status.serialize(self._serialize_rrset_info, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                    if nsec_serialized:
                        d['wildcard_proof'][wildcard_name_str].append(nsec_serialized)
                if not d['wildcard_proof'][wildcard_name_str]:
                    del d['wildcard_proof'][wildcard_name_str]
            if not d['wildcard_proof']:
                del d['wildcard_proof']

        if self.rrset_warnings[rrset_info] and loglevel <= logging.WARNING:
            d['warnings'] = [w.serialize(consolidate_clients=consolidate_clients, html_format=html_format) for w in self.rrset_warnings[rrset_info]]

        if self.rrset_errors[rrset_info] and loglevel <= logging.ERROR:
            d['errors'] = [e.serialize(consolidate_clients=consolidate_clients, html_format=html_format) for e in self.rrset_errors[rrset_info]]

        return d

    def _serialize_negative_response_info(self, neg_response_info, neg_status, warnings, errors, consolidate_clients=False, loglevel=logging.DEBUG, html_format=False):
        d = collections.OrderedDict()

        d['proof'] = []
        for nsec_status in neg_status[neg_response_info]:
            nsec_serialized = nsec_status.serialize(self._serialize_rrset_info, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
            if nsec_serialized:
                d['proof'].append(nsec_serialized)
        if not d['proof']:
            del d['proof']

        d['soa'] = []
        for soa_rrset_info in neg_response_info.soa_rrset_info:
            rrset_serialized = self._serialize_rrset_info(soa_rrset_info, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
            if rrset_serialized:
                d['soa'].append(rrset_serialized)
        if not d['soa']:
            del d['soa']

        if loglevel <= logging.DEBUG or \
                (warnings[neg_response_info] and loglevel <= logging.WARNING) or \
                (errors[neg_response_info] and loglevel <= logging.ERROR):
            servers = tuple_to_dict(neg_response_info.servers_clients)
            if consolidate_clients:
                servers = list(servers)
                servers.sort()
            d['servers'] = servers

        if warnings[neg_response_info] and loglevel <= logging.WARNING:
            d['warnings'] = [w.serialize(consolidate_clients=consolidate_clients, html_format=html_format) for w in warnings[neg_response_info]]

        if errors[neg_response_info] and loglevel <= logging.ERROR:
            d['errors'] = [e.serialize(consolidate_clients=consolidate_clients, html_format=html_format) for e in errors[neg_response_info]]

        return d

    def _serialize_query_status(self, query, consolidate_clients=False, loglevel=logging.DEBUG, html_format=False):
        d = collections.OrderedDict()
        d['answer'] = []
        d['nxdomain'] = []
        d['nodata'] = []
        d['error'] = []

        #TODO sort by CNAME dependencies, beginning with question
        for rrset_info in query.answer_info:
            # only look at qname
            #TODO fix this check for recursive
            if rrset_info.rrset.name == query.qname:
                rrset_serialized = self._serialize_rrset_info(rrset_info, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                if rrset_serialized:
                    d['answer'].append(rrset_serialized)

        for neg_response_info in query.nxdomain_info:
            # only look at qname
            #TODO fix this check for recursive
            if neg_response_info.qname == query.qname:
                neg_response_serialized = self._serialize_negative_response_info(neg_response_info, self.nxdomain_status, self.nxdomain_warnings, self.nxdomain_errors, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                if neg_response_serialized:
                    d['nxdomain'].append(neg_response_serialized)

        for neg_response_info in query.nodata_info:
            # only look at qname
            #TODO fix this check for recursive
            if neg_response_info.qname == query.qname:
                neg_response_serialized = self._serialize_negative_response_info(neg_response_info, self.nodata_status, self.nodata_warnings, self.nodata_errors, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                if neg_response_serialized:
                    d['nodata'].append(neg_response_serialized)

        for error in self.response_errors[query]:
            error_serialized = error.serialize(consolidate_clients=consolidate_clients, html_format=html_format)
            if error_serialized:
                d['error'].append(error_serialized)

        if not d['answer']: del d['answer']
        if not d['nxdomain']: del d['nxdomain']
        if not d['nodata']: del d['nodata']
        if not d['error']: del d['error']

        return d

    def _serialize_dnskey_status(self, consolidate_clients=False, loglevel=logging.DEBUG, html_format=False):
        d = []

        for dnskey in self.get_dnskeys():
            dnskey_serialized = dnskey.serialize(consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
            if dnskey_serialized:
                d.append(dnskey_serialized)

        return d

    def _serialize_delegation_status(self, rdtype, consolidate_clients=False, loglevel=logging.DEBUG, html_format=False):
        d = collections.OrderedDict()

        dss = self.ds_status_by_ds[rdtype].keys()
        d['ds'] = []
        dss.sort()
        for ds in dss:
            dnskeys = self.ds_status_by_ds[rdtype][ds].keys()
            dnskeys.sort()
            for dnskey in dnskeys:
                ds_status = self.ds_status_by_ds[rdtype][ds][dnskey]
                ds_serialized = ds_status.serialize(consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                if ds_serialized:
                    d['ds'].append(ds_serialized)
        if not d['ds']:
            del d['ds']

        try:
            neg_response_info = filter(lambda x: x.qname == self.name and x.rdtype == rdtype, self.nodata_status)[0]
            status = self.nodata_status
        except IndexError:
            try:
                neg_response_info = filter(lambda x: x.qname == self.name and x.rdtype == rdtype, self.nxdomain_status)[0]
                status = self.nxdomain_status
            except IndexError:
                neg_response_info = None

        if neg_response_info is not None:
            d['insecurity_proof'] = []
            for nsec_status in status[neg_response_info]:
                nsec_serialized = nsec_status.serialize(self._serialize_rrset_info, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                if nsec_serialized:
                    d['insecurity_proof'].append(nsec_serialized)
            if not d['insecurity_proof']:
                del d['insecurity_proof']

        if loglevel <= logging.INFO or self.delegation_status[rdtype] not in (Status.DELEGATION_STATUS_SECURE, Status.DELEGATION_STATUS_INSECURE):
            d['status'] = Status.delegation_status_mapping[self.delegation_status[rdtype]]

        if self.delegation_warnings[rdtype] and loglevel <= logging.WARNING:
            d['warnings'] = [w.serialize(consolidate_clients=consolidate_clients, html_format=html_format) for w in self.delegation_warnings[rdtype]]

        if self.delegation_errors[rdtype] and loglevel <= logging.ERROR:
            d['errors'] = [e.serialize(consolidate_clients=consolidate_clients, html_format=html_format) for e in self.delegation_errors[rdtype]]

        return d

    def serialize_status(self, d=None, is_dlv=False, loglevel=logging.DEBUG, level=RDTYPES_ALL, trace=None, follow_mx=True, html_format=False):
        if d is None:
            d = collections.OrderedDict()

        if trace is None:
            trace = []

        # avoid loops
        if self in trace:
            return d

        # if we're a stub, there's no status to serialize
        if self.stub:
            return d

        name_str = self.name.canonicalize().to_text()
        if name_str in d:
            return d

        # serialize status of dependencies first because their version of the
        # analysis might be the most complete (considering re-dos)
        if level <= self.RDTYPES_NS_TARGET:
            for cname in self.cname_targets:
                for target, cname_obj in self.cname_targets[cname].items():
                    cname_obj.serialize_status(d, loglevel=loglevel, level=max(self.RDTYPES_ALL_SAME_NAME, level), trace=trace + [self], html_format=html_format)
            if follow_mx:
                for target, mx_obj in self.mx_targets.items():
                    if mx_obj is not None:
                        mx_obj.serialize_status(d, loglevel=loglevel, level=max(self.RDTYPES_ALL_SAME_NAME, level), trace=trace + [self], follow_mx=False, html_format=html_format)
        if level <= self.RDTYPES_SECURE_DELEGATION:
            for signer, signer_obj in self.external_signers.items():
                signer_obj.serialize_status(d, loglevel=loglevel, level=self.RDTYPES_SECURE_DELEGATION, trace=trace + [self], html_format=html_format)
            for target, ns_obj in self.ns_dependencies.items():
                if ns_obj is not None:
                    ns_obj.serialize_status(d, loglevel=loglevel, level=self.RDTYPES_NS_TARGET, trace=trace + [self], html_format=html_format)

        # serialize status of ancestry
        if level <= self.RDTYPES_SECURE_DELEGATION:
            if self.parent is not None:
                self.parent.serialize_status(d, loglevel=loglevel, level=self.RDTYPES_SECURE_DELEGATION, trace=trace + [self], html_format=html_format)
            if self.dlv_parent is not None:
                self.dlv_parent.serialize_status(d, is_dlv=True, loglevel=loglevel, level=self.RDTYPES_SECURE_DELEGATION, trace=trace + [self], html_format=html_format)

        consolidate_clients = self.single_client()

        d[name_str] = collections.OrderedDict()
        if loglevel <= logging.INFO or self.status not in (Status.NAME_STATUS_NOERROR, Status.NAME_STATUS_NXDOMAIN):
            d[name_str]['status'] = Status.name_status_mapping[self.status]

        d[name_str]['queries'] = collections.OrderedDict()
        query_keys = self.queries.keys()
        query_keys.sort()
        required_rdtypes = self._rdtypes_for_analysis_level(level)
        for (qname, rdtype) in query_keys:

            if level > self.RDTYPES_ALL and qname not in (self.name, self.dlv_name):
                continue

            if required_rdtypes is not None and rdtype not in required_rdtypes:
                continue

            query_serialized = self._serialize_query_status(self.queries[(qname, rdtype)], consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
            if query_serialized:
                qname_type_str = '%s/%s/%s' % (qname.canonicalize().to_text(), dns.rdataclass.to_text(dns.rdataclass.IN), dns.rdatatype.to_text(rdtype))
                d[name_str]['queries'][qname_type_str] = query_serialized

        if not d[name_str]['queries']:
            del d[name_str]['queries']

        if level <= self.RDTYPES_SECURE_DELEGATION and (self.name, dns.rdatatype.DNSKEY) in self.queries:
            dnskey_serialized = self._serialize_dnskey_status(consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
            if dnskey_serialized:
                d[name_str]['dnskey'] = dnskey_serialized

        if self.is_zone():
            if self.parent is not None and not is_dlv:
                delegation_serialized = self._serialize_delegation_status(dns.rdatatype.DS, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                if delegation_serialized:
                    d[name_str]['delegation'] = delegation_serialized

            if self.dlv_parent is not None:
                if (self.dlv_name, dns.rdatatype.DLV) in self.queries:
                    delegation_serialized = self._serialize_delegation_status(dns.rdatatype.DLV, consolidate_clients=consolidate_clients, loglevel=loglevel, html_format=html_format)
                    if delegation_serialized:
                        d[name_str]['dlv'] = delegation_serialized

        if not d[name_str]:
            del d[name_str]

        return d
