"""Generate an explicit destination allowlist for isolated UI browsers."""

import argparse
import ipaddress
from pathlib import Path
import re


def configuration(hosts, ports):
    addresses, domains = [], []
    for host in hosts:
        host = host.strip().lower()
        try:
            addresses.append(str(ipaddress.ip_address(host)))
        except ValueError:
            if len(host) > 253 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host) or '..' in host:
                raise ValueError('Only exact IP addresses or domain names are allowed')
            domains.append(host)
    ports = sorted(set(int(port) for port in ports))
    if not (addresses or domains) or not ports or any(not 1 <= port <= 65535 for port in ports):
        raise ValueError('Specify at least one destination and valid port')
    lines = ['http_port 3128', 'visible_hostname liema-ui-proxy', 'pid_filename /tmp/squid.pid',
             'coredump_dir /tmp', 'cache deny all', 'cache_mem 0 MB', 'access_log none',
             'cache_log /dev/null', 'cache_store_log none', 'logfile_rotate 0',
             'pinger_enable off', 'shutdown_lifetime 1 seconds',
             'acl approved_ports port ' + ' '.join(map(str, ports))]
    if addresses:
        lines += ['acl approved_addresses dst ' + ' '.join(sorted(set(addresses))),
                  'http_access allow approved_addresses approved_ports']
    if domains:
        lines += ['acl approved_domains dstdomain ' + ' '.join(sorted(set(domains))),
                  'http_access allow approved_domains approved_ports']
    return '\n'.join(lines + ['http_access deny all', ''])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-host', action='append', required=True)
    parser.add_argument('--allow-port', action='append', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(configuration(args.allow_host, args.allow_port), encoding='utf-8')


if __name__ == '__main__':
    main()
