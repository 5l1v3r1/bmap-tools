""" This module allows opening and reading local and remote files and
decompress them on-the-fly if needed. Remote files are read using urllib2
(except of "ssh://" URLs, which are handled differently). Supported
compression types are: 'bz2', 'gz', 'tar.gz', 'tgz', 'tar.bz2'. """

import os
import errno
import urlparse

# Disable the following pylint errors and recommendations:
#   * Instance of X has no member Y (E1101), because it produces
#     false-positives for many of 'subprocess' class members, e.g.
#     "Instance of 'Popen' has no 'wait' member".
#   * Too many instance attributes (R0902)
# pylint: disable=E1101
# pylint: disable=R0902

# A list of supported compression types
SUPPORTED_COMPRESSION_TYPES = ('bz2', 'gz', 'tar.gz', 'tgz', 'tar.bz2')

def _fake_seek_forward(file_obj, cur_pos, offset, whence = os.SEEK_SET):
    """ This function implements the 'seek()' method for file object
    'file_obj'. Only seeking forward and is allowed, and 'whence' may be
    either 'os.SEEK_SET' or 'os.SEEK_CUR'. """

    if whence == os.SEEK_SET:
        new_pos = offset
    elif whence == os.SEEK_CUR:
        new_pos = cur_pos + offset
    else:
        raise Error("'seek()' method requires the 'whence' argument " \
                    "to be %d or %d, but %d was passed" \
                    % (os.SEEK_SET, os.SEEK_CUR, whence))

    if new_pos < cur_pos:
        raise Error("''seek()' method supports only seeking forward, " \
                    "seeking from %d to %d is not allowed" \
                    % (cur_pos, new_pos))

    length = new_pos - cur_pos
    to_read = length
    while to_read > 0:
        chunk_size = min(to_read, 1024 * 1024)
        buf = file_obj.read(chunk_size)
        if not buf:
            break
        to_read -= len(buf)

    if to_read < 0:
        raise Error("seeked too far: %d instead of %d" \
                    % (new_pos - to_read, new_pos))

    return new_pos - to_read

class Error(Exception):
    """ A class for exceptions generated by this module. We currently support
    only one type of exceptions, and we basically throw human-readable problem
    description in case of errors. """
    pass

class _CompressedFile:
    """ This class implements transparent reading from a compressed file-like
    object and decompressing its contents on-the-fly. """

    def __init__(self, file_obj, decompress_func = None, chunk_size = None):
        """ Class constructor. The 'file_ojb' argument is the compressed
        file-like object to read from. The 'decompress_func()' function is a
        function to use for decompression.

        The 'chunk_size' parameter may be used to limit the amount of data read
        from the input file at a time and it is assumed to be used with
        compressed files. This parameter has a big effect on the memory
        consumption in case the input file is a compressed stream of all
        zeroes. If we read a big chunk of such a compressed stream and
        decompress it, the length of the decompressed buffer may be huge. For
        example, when 'chunk_size' is 128KiB, the output buffer for a 4GiB .gz
        file filled with all zeroes is about 31MiB. Bzip2 is more dangerous -
        when 'chunk_size' is only 1KiB, the output buffer for a 4GiB .bz2 file
        filled with all zeroes is about 424MiB and when 'chunk_size' is 128
        bytes it is about 77MiB. """

        self._file_obj = file_obj
        self._decompress_func = decompress_func
        if chunk_size:
            self._chunk_size = chunk_size
        else:
            self._chunk_size = 128 * 1024
        self._pos = 0
        self._buffer = ''
        self._buffer_pos = 0
        self._eof = False

    def seek(self, offset, whence = os.SEEK_SET):
        """ The 'seek()' method, similar to the one file objects have. """

        self._pos = _fake_seek_forward(self, self._pos, offset, whence)

    def tell(self):
        """ The 'tell()' method, similar to the one file objects have. """

        return self._pos

    def _read_from_buffer(self, length):
        """ Read from the internal buffer. """

        buffer_len = len(self._buffer)
        if buffer_len - self._buffer_pos > length:
            data = self._buffer[self._buffer_pos:self._buffer_pos + length]
            self._buffer_pos += length
        else:
            data = self._buffer[self._buffer_pos:]
            self._buffer = ''
            self._buffer_pos = 0

        return data

    def read(self, size):
        """ Read the compressed file, uncompress the data on-the-fly, and
        return 'size' bytes of the uncompressed data. """

        assert self._pos >= 0
        assert self._buffer_pos >= 0
        assert self._buffer_pos <= len(self._buffer)

        if self._eof:
            return ''

        # Fetch the data from the buffers first
        data = self._read_from_buffer(size)
        size -= len(data)

        # If the buffers did not contain all the requested data, read them,
        # decompress, and buffer.

        while size > 0:
            buf = self._file_obj.read(self._chunk_size)
            if not buf:
                self._eof = True
                break

            if self._decompress_func:
                buf = self._decompress_func(buf)
                if not buf:
                    continue

            assert len(self._buffer) == 0
            assert self._buffer_pos == 0

            # The decompressor may return more data than we requested. Save the
            # extra data in an internal buffer.
            if len(buf) >= size:
                self._buffer = buf
                data += self._read_from_buffer(size)
            else:
                data += buf

            size -= len(buf)

        self._pos += len(data)

        return data

    def close(self):
        """ Close the '_CompressedFile' file-like object. """
        pass

def _decode_sshpass_exit_code(code):
    """ A helper function which converts "sshpass" command-line tool's exit code
    into a human-readable string. See "man sshpass". """

    if code == 1:
        result = "invalid command line argument"
    elif code == 2:
        result = "conflicting arguments given"
    elif code == 3:
        result = "general run-time error"
    elif code == 4:
        result = "unrecognized response from ssh (parse error)"
    elif code == 5:
        result = "invalid/incorrect password"
    elif code == 6:
        result = "host public key is unknown. sshpass exits without " \
                 "confirming the new key"
    elif code == 255:
        # SSH result =s 255 on any error
        result = "ssh error"
    else:
        result = "unknown"

    return result

class TransRead:
    """ This class implement the transparent reading functionality. Instances
    of this class are file-like objects which you can read and seek only
    forward.
    """

    def _open_compressed_file(self):
        """ Detect file compression type and open it with the corresponding
        compression module, or just plain 'open() if the file is not
        compressed. """

        try:
            if self.name.endswith('.tar.gz') \
               or self.name.endswith('.tar.bz2') \
               or self.name.endswith('.tgz'):
                import tarfile

                tar = tarfile.open(fileobj = self._file_obj, mode = 'r|*')
                member = tar.next()
                self._transfile_obj = tar.extractfile(member)
                self.size = member.size
            elif self.name.endswith('.gz'):
                import zlib

                decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
                self._transfile_obj = _CompressedFile(self._file_obj,
                                                      decompressor.decompress)
            elif self.name.endswith('.bz2'):
                import bz2

                self._transfile_obj = _CompressedFile(self._file_obj,
                                              bz2.BZ2Decompressor().decompress,
                                              128)
            else:
                self.is_compressed = False
                self._transfile_obj = self._file_obj
                if not self.is_url:
                    self.size = os.fstat(self._file_obj.fileno()).st_size
                self._file_obj = None
        except IOError as err:
            raise Error("cannot open file '%s': %s" % (self.name, err))

    def _open_url_ssh(self, url):
        """ This function opens a file on a remote host using SSH. The URL has
        to have this format: "ssh://username@hostname:path". Currently we only
        support password-based authentication. """

        import subprocess

        # Parse the URL
        parsed_url = urlparse.urlparse(url)
        username = parsed_url.username
        password = parsed_url.password
        path = parsed_url.path
        hostname = parsed_url.hostname
        if username:
            hostname = username + "@" + hostname

        # Make sure the ssh client program is installed
        try:
            subprocess.Popen("ssh", stderr = subprocess.PIPE,
                             stdout = subprocess.PIPE).wait()
        except OSError as err:
            if err.errno == os.errno.ENOENT:
                raise Error("\"sshpass\" program not found, but it is " \
                            "required for downloading over SSH")

        # Prepare the commands that we are going to run
        if password:
            # In case of password we have to use the sshpass tool to pass the
            # password to the ssh client utility
            popen_args = ["sshpass",
                          "-p" + password,
                          "ssh",
                          "-o StrictHostKeyChecking=no",
                          "-o PubkeyAuthentication=no",
                          "-o PasswordAuthentication=yes",
                          hostname]

            # Make sure the sshpass program is installed
            try:
                subprocess.Popen("sshpass", stderr = subprocess.PIPE,
                                 stdout = subprocess.PIPE).wait()
            except OSError as err:
                if err.errno == os.errno.ENOENT:
                    raise Error("\"sshpass\" program not found, but it is " \
                                "required for password SSH authentication")
        else:
            popen_args = ["ssh",
                          "-o StrictHostKeyChecking=no",
                          "-o PubkeyAuthentication=yes",
                          "-o PasswordAuthentication=no",
                          "-o BatchMode=yes",
                          hostname]

        # Test if we can successfully connect
        child_process = subprocess.Popen(popen_args + ["true"])
        child_process.wait()
        retcode = child_process.returncode
        if retcode != 0:
            decoded = _decode_sshpass_exit_code(retcode)
            raise Error("cannot connect to \"%s\": %s (error code %d)" % \
                        (hostname, decoded, retcode))

        # Test if file exists by running "test -f path && test -r path" on the
        # host
        command = "test -f " + path + " && test -r " + path
        child_process = subprocess.Popen(popen_args + [command],
                                         stdout = subprocess.PIPE)
        child_process.wait()
        if child_process.returncode != 0:
            raise Error("\"%s\" on \"%s\" cannot be read: make sure it " \
                        "exists, is a regular file, and you have read "   \
                        "permissions" % (path, hostname))

        # Read the entire file using 'cat'
        self._child_process = subprocess.Popen(popen_args + ["cat " + path],
                                               stdout = subprocess.PIPE)

        # Now the contents of the file should be available from sub-processes
        # stdout
        self._file_obj = self._child_process.stdout

        self.is_url = True
        self._force_fake_seek = True

    def _open_url(self, url):
        """ Open an URL 'url' and return the file-like object of the opened
        URL. """

        import urllib2
        import httplib

        parsed_url = urlparse.urlparse(url)
        username = parsed_url.username
        password = parsed_url.password

        if parsed_url.scheme == "ssh":
            # Unfortunately, liburl2 does not handle "ssh://" URLs
            self._open_url_ssh(url)
            return

        if username and password:
            # Unfortunately, in order to handle URLs which contain user name
            # and password (e.g., http://user:password@my.site.org), we need to
            # do few extra things.
            new_url = list(parsed_url)
            if parsed_url.port:
                new_url[1] = "%s:%s" % (parsed_url.hostname, parsed_url.port)
            else:
                new_url[1] = parsed_url.hostname
            url = urlparse.urlunparse(new_url)

            # Build an URL opener which will do the authentication
            password_manager = urllib2.HTTPPasswordMgrWithDefaultRealm()
            password_manager.add_password(None, url, username, password)
            auth_handler = urllib2.HTTPBasicAuthHandler(password_manager)
            opener = urllib2.build_opener(auth_handler)
        else:
            opener = urllib2.build_opener()

        opener.addheaders = [('User-Agent', 'Mozilla/5.0')]
        urllib2.install_opener(opener)

        try:
            self._file_obj = opener.open(url)
            self.is_url = True
        except (IOError, ValueError, httplib.InvalidURL) as err:
            raise Error("cannot open URL '%s': %s" % (url, err))
        except httplib.BadStatusLine:
            raise Error("cannot open URL '%s': server responds with an HTTP " \
                        "status code that we don't understand" % url)

    def _create_local_copy(self):
        """ Create a local copy of a remote or compressed file. """

        import tempfile

        try:
            tmp_file_obj = tempfile.NamedTemporaryFile("w+")
        except IOError as err:
            raise Error("cannot create a temporary file: %s" % err)

        while True:
            chunk = self.read(1024 * 1024)
            if not chunk:
                break

            tmp_file_obj.write(chunk)

        tmp_file_obj.flush()
        self.close()
        self.is_compressed = False
        self.is_url = False
        self._file_obj = tmp_file_obj

        try:
            self._transfile_obj = open(tmp_file_obj.name, "rb")
        except IOError as err:
            raise Error("cannot open own temporary file '%s': %s" \
                        % (tmp_file_obj.name, err))

    def __init__(self, filepath, local = False):
        """ Class constructor. The 'filepath' argument is the full path to the
        file to read transparently. If 'local' is True, then the file-like
        object is guaranteed to be backed by a local file. This means that if
        the source file is compressed or an URL, then it will first be copied
        to a temporary local file, and then all the subsequent operations will
        be done with the local copy. """

        self.name = filepath
        self.size = None
        self.is_compressed = True
        self.is_url = False
        self._child_process = None
        self._file_obj = None
        self._transfile_obj = None
        self._force_fake_seek = False
        self._pos = 0

        try:
            self._file_obj = open(self.name, "rb")
        except IOError as err:
            if err.errno == errno.ENOENT:
                # This is probably an URL
                self._open_url(filepath)
            else:
                raise Error("cannot open file '%s': %s" % (filepath, err))

        self._open_compressed_file()

        if local:
            self._create_local_copy()

    def read(self, size = -1):
        """ Read the data from the file or URL and and uncompress it on-the-fly
        if necessary. """

        if size < 0:
            size = 0xFFFFFFFFFFFFFFFF

        buf = self._transfile_obj.read(size)
        self._pos += len(buf)

        return buf

    def __del__(self):
        """ The class destructor which closes opened files. """

        if self._transfile_obj:
            self._transfile_obj.close()
        if self._file_obj:
            self._file_obj.close()
        if self._child_process:
            self._child_process.wait()

    def seek(self, offset, whence = os.SEEK_SET):
        """ The 'seek()' method, similar to the one file objects have. """

        if self._force_fake_seek or not hasattr(self._transfile_obj, "seek"):
            self._pos = _fake_seek_forward(self._transfile_obj, self._pos, \
                                           offset, whence)
        else:
            self._transfile_obj.seek(offset, whence)

    def tell(self):
        """ The 'tell()' method, similar to the one file objects have. """

        if self._force_fake_seek or not hasattr(self._transfile_obj, "tell"):
            return self._pos
        else:
            return self._transfile_obj.tell()

    def close(self):
        """ Close the file-like object. """

        self.__del__()

    def __getattr__(self, name):
        """ If we are backed by a local uncompressed file, then fall-back to
        using its operations. """

        if not self.is_compressed and not self.is_url:
            return getattr(self._transfile_obj, name)
        else:
            raise AttributeError
