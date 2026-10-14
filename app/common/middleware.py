"""Response compression for the dynamic pages and JSON the dashboard serves.

The HTML pages carry a lot of repetitive markup and embedded JSON (a Home page is ~140 KB, the
Daily Comparison page ~350 KB) that compresses roughly 7-15x, which is most of the transfer time on
a slow link. Static files are already served pre-compressed by whitenoise (it sits before this in
MIDDLEWARE and answers them without reaching it); the browsers' `Accept-Encoding: gzip` decides
whether anything is compressed at all, so clients that do not ask get the plain body.
"""

from __future__ import annotations

from django.middleware.gzip import GZipMiddleware


class CompressMiddleware(GZipMiddleware):
    """Django's GZipMiddleware, except for server-sent events (the live pipeline stream), which must
    reach the browser as each event is produced and must not be buffered by a compressor."""

    def process_response(self, request, response):
        if response.get("Content-Type", "").startswith("text/event-stream"):
            return response
        return super().process_response(request, response)
