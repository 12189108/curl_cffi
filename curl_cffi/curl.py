import os
import re
import warnings
from http.cookies import SimpleCookie
from typing import Any, List, Union

from ._wrapper import ffi, lib  # type: ignore
from .const import CurlInfo, CurlOpt

DEFAULT_CACERT = os.path.join(os.path.dirname(__file__), "cacert.pem")


class CurlError(Exception):
    """Base exception for curl_cffi package"""
    pass


CURLINFO_TEXT = 0
CURLINFO_HEADER_IN = 1
CURLINFO_HEADER_OUT = 2
CURLINFO_DATA_IN = 3
CURLINFO_DATA_OUT = 4
CURLINFO_SSL_DATA_IN = 5
CURLINFO_SSL_DATA_OUT = 6
CURLINFO_DATA_OUT = 4


@ffi.def_extern()
def debug_function(curl, type: int, data, size, clientp) -> int:
    text = ffi.buffer(data, size)[:]
    if type == 0:
        print("CURLINFO", text)
    elif type == 2:
        print("HEADER OUT", text)
    elif type == 4:
        print("DATA OUT", text)
    elif type == 6:
        print("SSL OUT", text)
    return 0


@ffi.def_extern()
def buffer_callback(ptr, size, nmemb, userdata):
    # assert size == 1
    buffer = ffi.from_handle(userdata)
    buffer.write(ffi.buffer(ptr, nmemb)[:])
    return nmemb * size


@ffi.def_extern()
def write_callback(ptr, size, nmemb, userdata):
    # although similar enough to the function above, kept here for performance reasons
    callback = ffi.from_handle(userdata)
    callback(ffi.buffer(ptr, nmemb)[:])
    return nmemb * size


class Curl:
    """
    Wrapper for `curl_easy_*` functions of libcurl.
    """
    def __init__(self, cacert: str = DEFAULT_CACERT, debug: bool = False):
        """
        Parameters:
            cacert: CA cert path to use, by default, curl_cffi uses its own bundled cert.
            debug: whether to show curl debug messages.
        """
        self._curl = lib.curl_easy_init()
        self._headers = ffi.NULL
        self._cacert = cacert
        self._is_cert_set = False
        self._write_handle = None
        self._header_handle = None
        # TODO: use CURL_ERROR_SIZE
        self._error_buffer = ffi.new("char[]", 256)
        self._debug = debug
        self._set_error_buffer()

    def _set_error_buffer(self):
        ret = lib._curl_easy_setopt(self._curl, CurlOpt.ERRORBUFFER, self._error_buffer)
        if ret != 0:
            warnings.warn("Failed to set error buffer")
        if self._debug:
            self.setopt(CurlOpt.VERBOSE, 1)
            lib._curl_easy_setopt(self._curl, CurlOpt.DEBUGFUNCTION, lib.debug_function)

    def __del__(self):
        self.close()

    def _check_error(self, errcode: int, action: str):
        error = self._get_error(errcode, action)
        if error is not None:
            raise error

    def _get_error(self, errcode: int, action: str):
        if errcode != 0:
            errmsg = ffi.string(self._error_buffer).decode()
            return CurlError(
                f"Failed to {action}, ErrCode: {errcode}, Reason: '{errmsg}'. "
                "This may be a libcurl error, "
                "See https://curl.se/libcurl/c/libcurl-errors.html first for more details."
            )

    def setopt(self, option: CurlOpt, value: Any):
        """Wrapper for curl_easy_setopt.

        Parameters:
            option: option to set, use the constants from CurlOpt enum
            value: value to set, strings will be handled automatically
        """
        input_option = {
            # this should be int in curl, but cffi requires pointer for void*
            # it will be convert back in the glue c code.
            0: "int*",
            10000: "char*",
            20000: "void*",
            30000: "int*",  # offset type
        }
        # print("option", option, "value", value)

        # Convert value
        value_type = input_option.get(int(option / 10000) * 10000)
        if value_type == "int*":
            c_value = ffi.new("int*", value)
        elif option == CurlOpt.WRITEDATA:
            c_value = ffi.new_handle(value)
            self._write_handle = c_value
            lib._curl_easy_setopt(
                self._curl, CurlOpt.WRITEFUNCTION, lib.buffer_callback
            )
        elif option == CurlOpt.HEADERDATA:
            c_value = ffi.new_handle(value)
            self._header_handle = c_value
            lib._curl_easy_setopt(
                self._curl, CurlOpt.HEADERFUNCTION, lib.buffer_callback
            )
        elif option == CurlOpt.WRITEFUNCTION:
            c_value = ffi.new_handle(value)
            self._write_handle = c_value
            lib._curl_easy_setopt(self._curl, CurlOpt.WRITEFUNCTION, lib.write_callback)
            option = CurlOpt.WRITEDATA
        elif option == CurlOpt.HEADERFUNCTION:
            c_value = ffi.new_handle(value)
            self._header_handle = c_value
            lib._curl_easy_setopt(self._curl, CurlOpt.WRITEFUNCTION, lib.write_callback)
            option = CurlOpt.HEADERDATA
        elif value_type == "char*":
            if isinstance(value, str):
                c_value = value.encode()
            else:
                c_value = value
            # Must keep a reference, otherwise may be GCed.
            if option == CurlOpt.POSTFIELDS:
                self._body_handle = c_value
        else:
            raise NotImplementedError("Option unsupported: %s" % option)

        if option == CurlOpt.HTTPHEADER:
            for header in value:
                self._headers = lib.curl_slist_append(self._headers, header)
            ret = lib._curl_easy_setopt(self._curl, option, self._headers)
        else:
            ret = lib._curl_easy_setopt(self._curl, option, c_value)
        self._check_error(ret, "setopt(%s, %s)" % (option, value))

        if option == CurlOpt.CAINFO:
            self._is_cert_set = True

        return ret

    def getinfo(self, option: CurlInfo) -> Union[bytes, int, float]:
        """Wrapper for curl_easy_getinfo. Gets information in response after curl perform.

        Parameters:
            option: option to get info of, use the constants from CurlInfo enum
        """
        ret_option = {
            0x100000: "char**",
            0x200000: "long*",
            0x300000: "double*",
        }
        ret_cast_option = {
            0x100000: ffi.string,
            0x200000: int,
            0x300000: float,
        }
        c_value = ffi.new(ret_option[option & 0xF00000])
        ret = lib.curl_easy_getinfo(self._curl, option, c_value)
        self._check_error(ret, action="getinfo(%s)" % option)
        if c_value[0] == ffi.NULL:
            return b""
        return ret_cast_option[option & 0xF00000](c_value[0])

    def version(self) -> bytes:
        """Get the underlying libcurl version."""
        return ffi.string(lib.curl_version())

    def impersonate(self, target: str, default_headers: bool = True) -> int:
        """Set the browser type to impersonate.

        Parameters:
            target: browser to impersonate.
            default_headers: whether to add default headers, like User-Agent.
        """
        return lib.curl_easy_impersonate(
            self._curl, target.encode(), int(default_headers)
        )

    def _ensure_cacert(self):
        if not self._is_cert_set:
            ret = self.setopt(CurlOpt.CAINFO, self._cacert)
            self._check_error(ret, action="set cacert")

    def perform(self, clear_headers: bool = True):
        """Wrapper for curl_easy_perform, performs a curl request.

        Parameters:
            clear_headers: clear header slist used in this perform
        """
        # make sure we set a cacert store
        self._ensure_cacert()

        # here we go
        ret = lib.curl_easy_perform(self._curl)

        try:
            self._check_error(ret, action="perform")
        finally:
            # cleaning
            self.clean_after_perform(clear_headers)

    def clean_after_perform(self, clear_headers: bool = True):
        """Clean up handles and buffers after perform, called at the end of `perform`."""
        self._write_handle = None
        self._header_handle = None
        self._body_handle = None
        if clear_headers:
            if self._headers != ffi.NULL:
                lib.curl_slist_free_all(self._headers)
            self._headers = ffi.NULL

    def reset(self):
        """Reset all curl options, wrapper for curl_easy_reset."""
        self._is_cert_set = False
        lib.curl_easy_reset(self._curl)
        self._set_error_buffer()

    def parse_cookie_headers(self, headers: List[bytes]) -> SimpleCookie:
        """Extract cookies.SimpleCookie from header lines.

        Parameters:
            headers: list of headers in bytes.

        Returns:
            A parsed cookies.SimpleCookie instance.
        """
        cookie = SimpleCookie()
        for header in headers:
            if header.lower().startswith(b"set-cookie: "):
                cookie.load(header[12:].decode())  # len("set-cookie: ") == 12
        return cookie

    def get_reason_phrase(self, status_line: bytes) -> bytes:
        """Extract reason phrase, like `OK`, `Not Found` from response status line."""
        m = re.match(rb"HTTP/\d\.\d [0-9]{3} (.*)", status_line)
        return m.group(1) if m else b""

    def close(self):
        """Close and cleanup curl handle, wrapper for curl_easy_cleanup"""
        if self._curl:
            lib.curl_easy_cleanup(self._curl)
            self._curl = None
        ffi.release(self._error_buffer)
