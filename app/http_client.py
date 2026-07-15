import requests
import stamina

# Found the hard way: a transient DNS resolution failure for
# ofsistorage.blob.core.windows.net (uk_ofsi.py's source) took down an
# entire scrape run with no actual bug involved. RequestException covers
# both connection-level failures (DNS, connection refused, timeouts) and
# the raise_for_status() call below, so retries cover 5xx responses too.
#
# One shared function, not a session per call — sources run concurrently in
# threads (see scraper.py), but each hits a different host (treasury.gov,
# fincen.gov, justice.gov, europol.eu, ec.europa.eu, azure blob storage), so
# there's no cross-source connection-pooling benefit either way.


@stamina.retry(on=requests.exceptions.RequestException, attempts=3)
def get(url, **kwargs):
    resp = requests.get(url, **kwargs)
    resp.raise_for_status()
    return resp
