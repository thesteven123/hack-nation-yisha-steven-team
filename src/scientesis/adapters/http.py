import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def open_without_redirects(request, timeout):
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)
