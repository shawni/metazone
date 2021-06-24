#!/usr/bin/python3

import dns.name
import dns.resolver
import dns.ipv4
import dns.ipv6
import dns.zone
import base64
import hashlib
import functools
import yaml
import sys

#
# mzlib: functions helpful for metazone creation/interpretation
#
# LM: 2021-06-24 11:47:49-07:00
# Shawn Instenes <sinstenes@gmail.com>
#
#


def d8(s: str) -> str:
    """
    Decode Unicode string with stanard options
    """
    return str(s, 'utf-8', 'ignore')


def e8(s: str) -> str:
    """
    Encode Unicode string with stanard options
    """
    return s.encode('utf-8', 'ignore')


@functools.lru_cache(maxsize=2000)
def cz_hash(x: str) -> str:
    """
    From draft-muks-dnsop-dns-catalog-zones-*: Section 2.5.6/Appendix A.
    40-digit hex SHA-1 hash of the RDATA of the RR in uncompressed wire format

    """
    n = dns.name.from_text(x)
    h = hashlib.sha1(n.to_wire()).hexdigest()
    # 160 bit hash / 4 bits-per-symbol == 40.
    return h.lower()


@functools.lru_cache(maxsize=2000)
def cz_hash32(x: str) -> str:
    """
    SHA-1 hash of the RDATA of the RR in uncompressed wire format

    Here, it's a base32 encoding of that hash.  Some DNS systems have
    a problem with multi-label names over 64 characters; this helps.

    """
    n = dns.name.from_text(x)
    h = hashlib.sha1(n.to_wire()).digest()
    # 160 bit hash / 5 bits-per-symbol == 32.  Avoid hashes not a multiple of 5 bits with b32encode()
    return d8(base64.b32encode(h)).lower()


def resolve_a(name: str, iptype: int = 4, debug: bool = False) -> str:
    """
    Generate answers for DNS A lookups, including fast bogus answers for debugging
    """
    print(name, iptype, debug)
    if debug:
        h = hashlib.sha1(e8(name)).digest()  # produces something that "looks like" an IP but fully reproducable
        if iptype == 4:
            yield dns.ipv4.inet_ntoa(h[0:4])
        else:
            yield dns.ipv6.inet_ntoa(h[0:16])
        return
    try:
        if iptype == 4:
            ans = dns.resolver.resolve(name, 'A')
        else:
            ans = dns.resolver.resolve(name, 'AAAA')
        for rdata in ans:
            yield rdata.address
    finally:
        return


@functools.lru_cache(maxsize=2000)
def gather_a(name: str, iptype: int = 4, debug: bool = False) -> str:
    """
    Create single string containing all IPs at a name, separated by spaces
    """
    results = []
    for ip in resolve_a(name, iptype, debug):
        results.append(ip)
    if len(results) > 0:
        return " ".join(results)
    else:
        return " "


@functools.lru_cache(maxsize=2000)
def search_address(name: str, search_path: str, preferv4: bool = True, debug: bool = False) -> str:
    """
    Find names, given a resolv.conf-style search path.
    """
    if preferv4:
        isearch = [4, 6]
    else:
        isearch = [6, 4]

    if name[-1] == '.':  # FQDN, don't search
        result = gather_a(name, isearch[0], debug)
        if result == " ":
            result = gather_a(name, isearch[1], debug)
        return result

    else:
        for suffix in search_path.split(" "):
            fqdn = gather_a(name + "." + suffix, isearch[0], debug)
            if fqdn != " ":
                return fqdn
            fqdn = gather_a(name + "." + suffix, isearch[1], debug)
            if fqdn != " ":
                return fqdn
    sys.stderr.write("Hostname not found: " + name + "\n")
    raise ValueError


def tree_lookup(yaml: str, key: str) -> str:
    """
    Find an entry in a dictionary of dictionary... using "/" as path separator.
    """
    if yaml is None:
        return None
    if key is None:
        return None
    try:
        ind = key.find("/")
    except Exception:
        ind = -1
    if ind >= 0:
        try:
            for subkey in key.split("/"):
                yaml = yaml[subkey]
            return yaml
        except Exception:
            return key
    else:
        try:
            return yaml[key]
        except Exception:
            return key


def fetch_subtree(yaml: str, key: str) -> str:
    """
    Generator to return all scalars under a subtree, including members dicts/lists
    """
    node = tree_lookup(yaml, key)
    if node is None:
        return
    elif isinstance(node, list):
        for ele in node:
            yield ele
    elif isinstance(node, dict):
        for subkey in node.keys():
            for ele in fetch_subtree(yaml, key + "/" + subkey):
                yield ele
    else:
        yield node


def subtree_collect(yaml: str, key: str, search_path: str) -> str:
    """
    Return all scalars in-and-under a node in the YAML tree
    """
    result = []
    for ele in fetch_subtree(yaml, key):
        itm = lookup(yaml, ele, search_path)
        result.append(str(itm))
    return " ".join(result)


def careful_gen_eval(yaml: str, key: str, search_path: str) -> str:
    """
    Carefully allow some limited python expressions in data
    """
    safe_things = {'yaml': yaml, 'search': search_path, 'lookup': lookup, 'nsg': nsg}
    return str(eval(key, {'__builtins__': None}, safe_things))


def careful_node_eval(key: str, namespace: str) -> str:
    """
    Carefully allow an end-node limited python expressions in data
    """
    safe_things = {'nsg': namespace}
    return str(eval(key, {'__builtins__': None}, safe_things))


def single_lookup(yaml: str, key: str, search_path: str, preferv4: bool = False, debug: bool = False) -> str:
    """
    Given a single key that might contain a method keyword, return appropriate scalar
    """
    try:
        ind = key.find(":")
    except:
        ind = -1
    if ind >= 0:
        (method, ws) = key.split(":", 1)
        if method == "key":
            return lookup(yaml, ws, search_path)
        if method == "eval":
            return careful_gen_eval(yaml, ws, search_path) 
        if method == "host":
            return search_address(ws, search_path, preferv4, debug)
        if method == "collect":
            return subtree_collect(yaml, ws, search_path)
        return key
    else:
        return tree_lookup(yaml, key)


def lookup(yaml: str, key: str, search_path: str, preferv4: bool = False, debug: bool = False) -> str:
    """
    Main YAML search/lookup function, supports methods
    """
    key = str(key)
    try:
        mk = key.split(' ')
    except:
        return single_lookup(yaml, key, search_path, preferv4, debug)
    result = []
    for sk in mk:
        itm = single_lookup(yaml, sk, search_path, preferv4, debug)
        result.append(str(itm))

    return " ".join(result)


def map_rrtype(rtype: str) -> str:
    """
    Properties of zones/nsgs are stored as the following RR types.
    """
    apl_types = ('allow-query', 'allow-transfer', 'allow-notify',
                 'allow-recursion', 'masters', 'also-notify-list',
                 'default-forward-list', 'forward-list', 'local-bridge',
                 'local0-apl', 'local1-apl', 'local2-apl')
    if rtype in apl_types:
        return "APL"
    else:
        return "TXT"


def apl_singleton(ip: str) -> str:
    """
    Convert a single IP or net/mask to APL item form
    """
    try:
        ind = ip.find(":")  # IPv6?
    except:
        ind = -1
    if ind >= 0:
        prefix = "1:"
    else:
        prefix = "2:"
    try:
        ind = ip.index("/")  # mask included?
    except:
        ind = -1
        if prefix == "1:":
            ip += "/32"
        else:
            ip += "/128"
    return prefix + ip


def generate_single_apl(prefixes: str) -> str:
    """
    Convert a list of prefixes/singles to an APL record
    """
    result = []
    try:
        ind = prefixes.index(" ")
        if ind >= 0:
            for sp in prefixes.split(" "):
                result.append(apl_singleton(sp))
        return " ".join(result)
    except:
        return apl_singleton(prefixes)


def generate_apl_list(yaml: str, thing: str, search_path: str) -> str:
    """
    Convert other things to APL format- hostnames (looked up), any/none, et cetera
    """
    result = []
    if thing == "any":
        return "1:0.0.0.0/0"
    if thing == "none":
        return "1:255.255.255.255/32"

    if str.isdigit(thing[0]):
        try:
            # might be a list of IPs
            ind = thing.index(" ")
            if ind >= 0:
                for itm in thing.split(" "):
                    result.append(generate_single_apl(itm))
                return " ".join(result)
            return generate_single_apl(thing)
        except:
            return generate_single_apl(thing)
    else:
        v = tree_lookup(yaml, thing)
        if v is None:
            addrs = search_address(thing, search_path)  # TODO preferv4/debug
            return generate_apl_list(yaml, addrs, search_path)  # might be a list of A/AAAA records
        else:
            for itm in v:
                result.append(generate_single_apl(itm))
            return " ".join(result)


def canonical_rr_format(yaml: str, rr: str, rrtype: str, search_path: str) -> str:
    """
    Make sure RR is in the canonical form
    """
    if rrtype == "APL":
        return generate_apl_list(yaml, rr, search_path)
    elif rrtype == "TXT":
        return '"' + rr + '"'
    elif rrtype == "PTR":
        if rr[-1] == ".":
            return rr
        else:
            return rr + "."
    else:
        return rr


def mz_emit_property(yaml: str, prpname: str, nsg: str, rrt: str, rrdata: str, search_path: str, rdatamax: int = 240) -> None:
    """
    Function to emit RFC-1035 "master file" format data
    """
    rrsplit = rrdata.split(" ")
    rrsplit.reverse()
    while len(rrsplit) > 0:
        result = []
        rdatalen = 0
        while len(rrsplit) > 0 and rdatalen < rdatamax:
            itm = rrsplit.pop()
            result.append(itm)
            rdatalen += len(itm) + 1
        rdatastr = " ".join(result)
        rdatastr = canonical_rr_format(yaml, rdatastr, rrt, search_path)
        if nsg == "":
            print(str.format('{0} 3600 IN {1} {2}', prpname, rrt, rdatastr))
        else:
            print(str.format('{0}.{3} 3600 IN {1} {2}', prpname, rrt, rdatastr, nsg))


def read_property(zone: dns.zone.Zone, name: str) -> str:
    """
    Return target of PTR or follow DNAME/CNAME to a PTR
    """
    try:
        for rdata in zone[name]:
            if rdata.rdtype == 12 or rdata.rdtype == 5 or rdata.rdtype == 39:   # PTR, CNAME, or DNAME
                for itm in rdata.items:
                    for ans in itm.target:
                        yield ans
    finally:
        return


def text_rrset(rr: dns.rdatatype) -> str:
    """
    Return TXT rdata sections joined back together
    """
    return " ".join(rr.strings)  # TODO space or zero string?


def config_string(zone: dns.zone.Zone, name: str) -> str:
    """
    Get the first value at the target TXT, APL node; follow CNAMEs if you must
    We ignore the case where node has both TXT and APL; first listed wins
    """
    for rdata in zone[name]:
        if rdata.rdtype == 16:  # TXT RR
            return " ".join(map(text_rrset, rdata.items))
        elif rdata.rdtype == 42:  # APL RR
            return " ".join(map(str, rdata.items))
        elif rdata.rdtype == 5:  # CNAME RR
            return config_string(zone, str(rdata.items[0].target))
        else:
            return str(rdata.items[0])


def config_lookup(key: str, d1: dict, d2: dict, d3: dict) -> str:
    """
    Given three dictionaries, look up a configuration string, allowing overrides
    d1 overrides d2 which overrides d3
    """
    return d1.get(key, d2.get(key, d3.get(key, "")))


def config_eval(key: str, d1: dict, d2: dict, d3: dict, namespace: str) -> str:
    """
    Look up a configuration parameter, providing for eval and late binding
    """
    ans = config_lookup(key, d1, d2, d3)
    try:
        ind = ans.find(":")
    except Exception:
        ind = -1
    if i >= 0:
        (method, st) = ans.split(":", 1)
        if method == "eval":
            return careful_node_eval(st, namespace)
        elif method == "fetch":
            return config_lookup(key, d1, d2, d3)
        else:
            return ans
    else:
        return ans


#############################################
# Tests

def main():

    tests = (
        'foo.bar.testing.com.',
        'foo.baz.testing.com.',
        'foo.bar.baz.testing.com.',
        'foo.baz.baz.testing.com.',
        'bar.bar.testing.com.',
        'bar.baz.testing.com.',
    )

    print("cz_hash32 test:")
    for t in tests:
        print("node.%s 3600 IN PTR %s" % (cz_hash32(t), t))

    print("Test of ipv4 normal search: %s" % search_address("www.yahoo.com.", '', True, False))
    print("Test of ipv4 debug  search: %s" % search_address("www.yahoo.com.", '', True, True))
    print("Test of ipv6 normal search: %s" % search_address("www.yahoo.com.", '', False, False))
    print("Test of ipv6 debug  search: %s" % search_address("www.yahoo.com.", '', False, True))

    try:
        y = yaml.safe_load(open("metazone.yaml", "r"))
    except:
        print("Error loading metazone.yaml\n")
        sys.exit(1)
    print("Defaults: ", lookup(y, "defaults", ""))


if __name__ == "__main__":
    main()