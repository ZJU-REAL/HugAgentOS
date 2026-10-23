"""Cookie isolation for external browser targets, independent of URL ports.

The current session cookie uses path '/'. Host-only cookies match the hostname;
Domain cookies also match subdomains. A new window never changes these rules.
"""

import ipaddress
from urllib.parse import urlsplit

from features.workbench import external


def validate_cookie_scope(request, target, config, cookie_domain):
    def normalize(host):
        value = host.encode("idna").decode("ascii").lower().rstrip(".")
        try:
            return ipaddress.ip_address(value).compressed
        except ValueError:
            return value

    domain = normalize(cookie_domain.lstrip(".")) if cookie_domain else None
    hosts = {normalize(request.url.hostname)}
    source = request.headers.get("origin")
    if source and urlsplit(source).hostname:
        hosts.add(normalize(urlsplit(source).hostname))
    urls = [target["url"]]
    if config.get("mode") == "exchange":
        urls.extend([config["initiationUrl"], config["callback"]])
    elif config.get("mode") == "shared_identity":
        urls.append(config["loginUrl"])
    for url in urls:
        external.validate_url(url)
        host = normalize(urlsplit(url).hostname)
        # Reject host page origin too: its cookies and browser storage are not
        # isolated merely by opening a separate window or choosing another port.
        if host in hosts or (domain and (host == domain or host.endswith("." + domain))):
            external.fail(409, "host_cookie_scope_overlap")
