"""Every tool page must load and run its startup JS without console errors."""
import pytest

PAGES = [
    "",                    # hub
    "topology.html",
    "wrap.html",
    "rig.html",
    "face.html",
    "cloth.html",
    "paint.html",
    "clean.html",
    "style-studio.html",
]


@pytest.mark.parametrize("path", PAGES)
def test_page_loads_without_errors(page, app_url, errors, path):
    # "commit", not "load"/"domcontentloaded": some pages pull in <model-viewer>
    # / deferred module scripts that keep those events pending far past what we
    # care about. Navigate on the response, then give the JS time to run.
    page.goto(app_url + "/" + path, wait_until="commit", timeout=60000)
    # let ES modules import, the CDN three.js load, and the first frame render
    page.wait_for_timeout(4000)
    assert errors == [], "%s produced errors:\n  %s" % (path or "index", "\n  ".join(errors))
