#!/usr/bin/env python3
from argparse import ArgumentParser
from enum import Enum
from fnmatch import fnmatch
import hashlib
import json
from mmap import mmap, ACCESS_READ
from os import fsdecode, fsencode, lstat, readlink, stat_result, getenv
from os.path import normpath
from pathlib import Path
import re
from stat import S_ISLNK, S_ISREG
from sys import stderr, exit

try:
    from scandir import walk
except ImportError:
    from os import walk

#TODO add argument to choose hash function? then also store in db? or just check format/length of hashes?
#TODO also add check when loading database that hash function specified matches hashes in db
#HASH_FUNCTION = hashlib.sha512
HASH_FUNCTION = hashlib.sha256 

HASH_NAME = HASH_FUNCTION().name
HASH_FILENAME = HASH_NAME.upper() + 'SUM'
#TODO why is this here. why not use arg.jsondb with a default value set in argparser?
DB_DEFAULT_FILENAME = getenv('HASH_DB_DEFAULT_FILE') if getenv('HASH_DB_DEFAULT_FILE') else 'hash_db.json'
# fnmatch patterns, specifically:
#TODO remove after add specification of specific hash file to import from
IMPORT_FILENAME_PATTERNS = [
    DB_DEFAULT_FILENAME,
    HASH_FILENAME,
    HASH_FILENAME + '.asc',
    '*.' + HASH_NAME + 'sum',
    '*.' + HASH_NAME + 'sum.asc',
    'DIGESTS',
    'DIGESTS.asc'
]
# 16 MiB, rounded down to a multiple of the hash's block size
CHUNK_SIZE = int(16777216 / HASH_FUNCTION().block_size) * HASH_FUNCTION().block_size
# Mostly used for importing from saved hash files
EMPTY_FILE_HASH = HASH_FUNCTION(b'').hexdigest()
SURROGATE_ESCAPES = re.compile(r'([\udc80-\udcff])')
HASH_PATTERN = re.compile(r'^[0-9a-fA-F]{' + str(HASH_FUNCTION().digest_size * 2) + '}$')

ADDED_COLOR = '\033[01;32m'
REMOVED_COLOR = '\033[01;34m'
MODIFIED_COLOR = '\033[01;31m'
NO_COLOR = '\033[00m'

# 1: 'version' field added
# 2: entry 'type' field added; symlinks now treated correctly
DATABASE_VERSION = 2

def read_saved_hashes(hash_file: Path) -> dict:
    hashes = {}
    with hash_file.open('rb') as f:
        for line in f:
            pieces = fsdecode(line).strip().split('  ', 1)
            if not HASH_PATTERN.match(pieces[0]):
                continue
            filename, file_hash = normpath(pieces[1]).replace('\\\\', '\\'), pieces[0]
            file_path = (hash_file.parent / filename).absolute()
            hashes[file_path] = file_hash
    return hashes

#TODO remove after add specification of specific hash file to import from. or could this be useful if you are aggregating several dirs, some of which have hashsum files in them but not all?
def find_external_hash_files(path: Path):
    for dirpath_str, _, filenames in walk(str(path)):
        dirpath = Path(dirpath_str).absolute()
        for filename in filenames:
            if any(fnmatch(filename, pattern) for pattern in IMPORT_FILENAME_PATTERNS):
                yield dirpath / filename

def find_hash_db_r(args, path: Path) -> Path:
    #TODO: don't search directories - use import for that?? or just require hashdb to be in root of data dir if you don't specify otherwise
    """
    Searches the given path and all of its parent
    directories to find a filename matching args.jsondb
    """
    abs_path = path.absolute()
    cur_path = abs_path / args.jsondb
    if cur_path.is_file():
        return cur_path
    parent = abs_path.parent
    if parent != abs_path:
        return find_hash_db_r(args, parent)

def find_hash_db(args, path: Path):
    hash_db_path = find_hash_db_r(args, path)
    if hash_db_path is None:
        message = "Couldn't find '{}' in '{}' or any parent directories"
        raise FileNotFoundError(message.format(args.jsondb, path))
    return hash_db_path

def split_path(path: Path):
    """
    :param path: Filesystem path
    :return: path pieces
    """
    return path.parts[1:]

class HashEntryType(Enum):
    TYPE_FILE = 0
    TYPE_SYMLINK = 1

class HashEntry:
    def __init__(self, filename, size=None, mtime=None, hash=None, type=None):
        # In memory, "filename" should be an absolute Path
        self.filename = filename
        self.size = size
        self.mtime = mtime
        self.hash = hash
        self.type = type

    def hash_file(self):
        if self.filename.is_file():
            if lstat(str(self.filename)).st_size > 0:
                with self.filename.open('rb') as f:
                    with mmap(f.fileno(), 0, access=ACCESS_READ) as m:
                        hash = HASH_FUNCTION()
                        for chunk in iter(lambda: m.read(CHUNK_SIZE), b''):
                            hash.update(chunk)

                        return hash.hexdigest()
            else:
                return EMPTY_FILE_HASH
        elif self.filename.is_symlink():
            # The link target will suffice as the "contents"
            target = readlink(str(self.filename))
            return HASH_FUNCTION(fsencode(target)).hexdigest()

    def exists(self):
        return self.filename.is_file() or self.filename.is_symlink()

    def verify(self):
        return self.hash_file() == self.hash

    def update_attrs(self):
        s = lstat(str(self.filename))
        self.size, self.mtime = s.st_size, s.st_mtime

    def update_type(self):
        if self.filename.is_symlink():
            self.type = HashEntryType.TYPE_SYMLINK
        else:
            # Treat it as a file even if it's missing. This only occurs when
            # importing from saved hashes.
            self.type = HashEntryType.TYPE_FILE

    def update(self):
        self.update_attrs()
        self.update_type()
        self.hash = self.hash_file()

    def __eq__(self, other):
        if isinstance(other, stat_result):
            return (
                self.size == other.st_size and
                self.mtime == other.st_mtime and
                (
                    (self.type == HashEntryType.TYPE_FILE and S_ISREG(other.st_mode))or
                    (self.type == HashEntryType.TYPE_SYMLINK and S_ISLNK(other.st_mode))
                )
            )
        return super().__eq__(other)

    def __hash__(self):
        return hash(self.filename)

def fix_symlinks(db):
    for entry in db.entries.values():
        if entry.type is None:
            entry.update_type()
            if entry.type == HashEntryType.TYPE_SYMLINK:
                entry.update()

# Intended usage: at version i, you need to run all
# upgrade functions in range(i, DATABASE_VERSION)
db_upgrades = [
    None,
    fix_symlinks,
]

class HashDatabase:
    def __init__(self, args, path: Path):
        self.args = args
        try:
            self.path = find_hash_db(args, path).parent
        except FileNotFoundError:
            self.path = path
        self.entries = {}
        self.version = DATABASE_VERSION
        self.info_url = "https://github.com/julowe/hash-db"

    def save(self):
        filename = self.path / self.args.jsondb
        data = {
            'info_url': self.info_url,
            'version': self.version,
            'files': {
                str(entry.filename.relative_to(self.path)): {
                    'size': entry.size,
                    'mtime': entry.mtime,
                    'hash': entry.hash,
                    'type': entry.type.value,
                }
                for entry in self.entries.values()
            }
        }
        with filename.open('w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, sort_keys=True)

    def split(self, subdir: Path):
        if subdir.is_file():
            raise NotADirectoryError(subdir)
        subdir = subdir.absolute()
        copy = self.__class__(self.path)
        copy.path = subdir
        pieces = split_path(subdir)
        prefix_len = len(pieces)
        for path, item in self.entries.items():
            entry_path_pieces = split_path(path)
            if pieces[:prefix_len] == entry_path_pieces[:prefix_len]:
                copy.entries[path] = item
        return copy

    def load(self):
        filename = find_hash_db(self.args, self.path)
        #TODO FIXME fails if not a json file
        with filename.open(encoding='utf-8') as f:
            data = json.load(f)
        #TODO do some basic checking of json structure to make sure it's not only a json file, but also correctly constructed for this program?
        self.version = data['version']
        #self.info_url = data['info_url'] #TODO decide if bumping version of DB is right, or if a check if this field exists before trying to load makes more sense. prob latter... or both? for now not as important, not reading in will just overwrite this field that isn't set by any other version of the script (yet)
        for filename, entry_data in data['files'].items():
            entry = HashEntry((self.path / filename).absolute())
            entry.size = entry_data.get('size')
            entry.mtime = entry_data.get('mtime')
            entry.hash = entry_data.get('hash')
            entry.type = HashEntryType(entry_data.get('type'))
            self.entries[entry.filename] = entry
        for i in range(self.version, DATABASE_VERSION):
            db_upgrades[i](self)
        self.version = DATABASE_VERSION

    def import_hashes(self, filename):
        """
        Imports a hash file created by e.g. sha512sum, and populates
        the database with this data. Examines each file to obtain the
        size and mtime information.

        Returns the number of file hashes imported.
        """
        hashes = read_saved_hashes(filename)
        i = 0
        for i, (file_path, hash) in enumerate(hashes.items(), 1):
            entry = HashEntry(file_path)
            entry.hash = hash
            entry.update_type()
            try:
                entry.update_attrs()
            except FileNotFoundError:
                # Not much else to do here.
                pass
            self.entries[entry.filename] = entry
        return i

    def _find_changes(self):
        """
        Walks the filesystem. Identifies noteworthy files -- those
        that were added, removed, or changed (size, mtime or type).

        Returns a 3-tuple of sets of HashEntry objects:
        [0] added files
        [1] removed files
        [2] modified files

        self.entries is not modified; this method only reports changes.
        """
        added = set()
        modified = set()
        existing_files = set()
        for dirpath_str, _, filenames in walk(str(self.path)):
            dirpath = Path(dirpath_str)
            for filename in filenames:
                #TODO either add SHA(256|512)SUM or expand to allow list of ignore files
                if filename == self.args.jsondb:
                    continue
                abs_filename = (dirpath / filename).absolute()
                if abs_filename in self.entries:
                    entry = self.entries[abs_filename]
                    existing_files.add(entry)
                    st = lstat(str(abs_filename))
                    if entry != st:
                        modified.add(entry)
                else:
                    try:
                        entry = HashEntry(abs_filename)
                        entry.update_attrs()
                        added.add(entry)
                    except FileNotFoundError:
                        # If file was removed between listing and processing,
                        # just treat it as if it never existed
                        # We have nothing to compare it to anyway
                        pass
        removed = set(self.entries.values()) - existing_files
        return added, removed, modified

    def update(self):
        """
        Walks the filesystem, adding and removing files from
        the database as appropriate.

        Returns a 3-tuple of sets of filenames:
        [0] added files
        [1] removed files
        [2] modified files
        """
        added, removed, modified = self._find_changes()
        # Make a new list of added files containing ones that
        # actually were added
        added_real = set()
        for entry in added:
            # If file was removed between listing and processing,
            # just treat it as if it never existed
            # We have nothing to compare it to anyway
            if entry.exists():
                entry.update()
                self.entries[entry.filename] = entry
                added_real.add(entry)
        for entry in removed:
            del self.entries[entry.filename]
        added = added_real
        # Entries will appear in 'modified' if the size, mtime or type
        # change. I've seen a lot of spurious mtime mismatches on vfat
        # filesystems (like on USB flash drives), so only report files
        # as modified if the hash changes.
        #TODO add err output that this occured? or only if mtime changed and hash didn't?
        content_modified = set()
        for entry in modified:
            old_hash = entry.hash
            entry.update()
            if entry.hash != old_hash:
                content_modified.add(entry)
        return (
            {entry.filename for entry in added},
            {entry.filename for entry in removed},
            {entry.filename for entry in content_modified},
        )

    def status(self):
        added, removed, modified = self._find_changes()
        return (
            {entry.filename for entry in added},
            {entry.filename for entry in removed},
            {entry.filename for entry in modified},
        )

    def verify(self, verbose_failures=False):
        """
        Calls each HashEntry's verify method to make sure that
        nothing has changed on disk.

        Returns a 2-tuple of sets of filenames:
        [0] modified files
        [1] removed files
        """
        modified = set()
        removed = set()
        count = len(self.entries)
        # TODO: Track number of bytes hashed instead of number of files
        # This will act as a more meaningful progress indicator
        i = 0
        for i, entry in enumerate(self.entries.values(), 1):
            if entry.exists():
                if entry.verify():
                    entry.update_attrs()
                else:
                    #TODO add 'very verbose' option? would output size and mod date of file from hash DB and what is on disk. and expected and returned hash?
                    if verbose_failures:
                        stderr.write('\r{} failed hash verification\n'.format(entry.filename))
                    modified.add(entry.filename)
            else:
                removed.add(entry.filename)
                if verbose_failures:
                    stderr.write('\r{} is missing\n'.format(entry.filename))
            stderr.write('\rChecked {} of {} files'.format(i, count))
        if i:
            stderr.write('\n')
        return modified, removed

    def export(self):
        """
        Exports the hash database in normal SHA512SUM format, usable as
        input to `sha512sum -c`

        Returns the number of entries exported.
        """
        hash_filename = self.path / HASH_FILENAME
        i = 0
        with hash_filename.open('wb') as f:
            for i, name in enumerate(sorted(self.entries), 1):
                entry = self.entries[name]
                filename = str(entry.filename.relative_to(self.path))
                line = entry.hash.encode('ascii') + b'  ' + fsencode(filename) + b'\n'
                f.write(line)
        return i

def print_file_list(files):
    for filename in sorted(files):
        printable_filename = SURROGATE_ESCAPES.sub('\ufffd', str(filename))
        print(printable_filename)
    print()

def print_file_lists(added, removed, modified):
    if added:
        print(ADDED_COLOR + 'Added files:' + NO_COLOR)
        print_file_list(added)
    if removed:
        print(REMOVED_COLOR + 'Removed files:' + NO_COLOR)
        print_file_list(removed)
    if modified:
        print(MODIFIED_COLOR + 'Modified files:' + NO_COLOR)
        print_file_list(modified)


##
## Wrapper Functions, called by command line arguments
##

def init(db, args):
    #TODO if allow relative data path to be stored in json file, then add switch here as to what version of db file to create. default to latest, but allow all. or just some?
    #TODO change to just checking for file existence? yup, fails if the file is not a hash db it can read. Is there a time when we care to specifically know that the file is a database, vs just that there is already a (any format) file named that?
    try:
        db.load()
        exit('Database exists, run update function instead. Stopping execution.')
        #exit('Filename exists, run update or status function instead. Stopping execution.')
    except FileNotFoundError:
        print('Initializing hash database')

    added, removed, modified = db.update()
    if args.verbose:
        print_file_lists(added, removed, modified)
    if not args.pretend:
        db.save()

def update(db, args):
    print('Updating hash database')
    db.load()
    added, removed, modified = db.update()
    if args.verbose:
        print_file_lists(added, removed, modified)
    if not args.pretend:
        db.save()

def status(db, args):
    db.load()
    added, removed, modified = db.status()
    print_file_lists(added, removed, modified)

def import_hashes(db, args):
    #TODO this could be borked if importing sha256sum and a hashdb with sha512 hashes. or would the hash just be read later as 'not matching' and recomputed?
    print('Importing hashes')
    overall_count = 0
    for import_filename in find_external_hash_files(Path().absolute()):
        if import_filename.name == args.jsondb:
            #TODO why would you use this import function on a json hash db?
            temp_db = HashDatabase(import_filename.parent)
            temp_db.load()
            count = len(temp_db.entries)
            db.entries.update(temp_db.entries)
        else:
            count = db.import_hashes(import_filename)
        overall_count += count
        print('Imported {} entries from {}'.format(count, import_filename))
    print('\nImported {} total entries'.format(overall_count))
    if not args.pretend:
        db.save()

def verify(db, args):
    db.load()
    modified, removed = db.verify(args.verbose_failures)
    print_file_lists(None, removed, modified)
    if args.update_mtimes and not args.pretend:
        db.save()

def split(db, args):
    db.load()
    new_db = db.split(args.subdir)
    new_db.save()
    print('Wrote {} hash entries to {}'.format(len(new_db.entries), new_db.path / args.jsondb))

def export(db, args):
    db.load()
    count = db.export()
    print('Exported {} entries to {}'.format(count, db.path / HASH_FILENAME))

if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('-d', '--data-dir', required='True', type=Path)
    #Hmm, so the json hash db has relative paths to files. so is -d required for all functions, or not for export? or also not for split with some reworking? or import? see TODO note below about mismatched json hash db files and data-dirs
    #TODO maybe change pretend to dry-run if it continues to trip me up when I read it
    parser.add_argument('-n', '--pretend', action='store_true')
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('-j', '--jsondb', help='JSON database file. Default: {}'.format(DB_DEFAULT_FILENAME), default=DB_DEFAULT_FILENAME)
    #TODO change -jsondb to a full path, not sure why you would want to go searching in the provided path and parent directories for a hash db... if you run it and your getcwd() is a subdir of where the database is, it still verifies/whatever all files in the database (not just those in cwd). not sure if I'm just missing the purpose so not changing yet
    #TODO hmm but allowing a full path for json db file then really opens up the need to check that the json hash db file is matched with the right data dir. If you run update on a hash db and point it to the wrong data dir, it will just list all files in db as removed, and all files in the (incorrect) data-dir as added... which could happen anyway if you move the json hash db, but maybe less likely??
    subparsers = parser.add_subparsers()

    parser_init = subparsers.add_parser('init')
    parser_init.set_defaults(func=init)

    parser_update = subparsers.add_parser('update')
    parser_update.set_defaults(func=update)

    parser_status = subparsers.add_parser('status')
    parser_status.set_defaults(func=status)

    parser_import = subparsers.add_parser('import')
    #TODO change to require that you specify path to import file. fails if hash database. can then remove IMPORT_FILENAME_PATTERNS
    parser_import.set_defaults(func=import_hashes)

    parser_verify = subparsers.add_parser('verify')
    parser_verify.add_argument('--verbose-failures', action='store_true', help=('If hash '
        'verification fails, print filenames as soon as they are known in addition '
        'to the post-hashing summary.'))
    parser_verify.add_argument('--update-mtimes', action='store_true', help=('If hash '
        'verification of a file succeeds, update its stored modification time to match '
        'that of the file on disk.'))
    parser_verify.set_defaults(func=verify)

    parser_split = subparsers.add_parser('split')
    parser_split.add_argument('subdir', type=Path)
    parser_split.set_defaults(func=split)

    parser_export = subparsers.add_parser('export')
    #TODO add ability to optionally specify output path
    parser_export.set_defaults(func=export)

    args = parser.parse_args()
    if "func" not in args:
        parser.print_help()
        exit(1)

    #TODO move this call to inside functions
    #ugh, yes this is redundant/weird/wrong, but this is the smallest change to make and get it tested and working reliably to handoff for next expedition in one day.
    #FIXME remove extra path passage, and just use args.data_dir everywhere
    db = HashDatabase(args, args.data_dir)
    args.func(db, args)
