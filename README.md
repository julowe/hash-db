This script creates and manages a simple database of file hashes. You can 
edit the code to use any of the alogrithms in 
[hashlib](https://docs.python.org/3/library/hashlib.html). This code was 
started by [mruffalo](https://github.com/mruffalo/hash-db), added to by 
[NHellFire](https://github.com/NHellFire/hash-db) and 
[pskarin](https://github.com/pskarin/hash-db), and merged together by 
[julowe](https://github.com/julowe/hash-db) with more additions to hopefully 
come soon.


Intro
=====

One could hash all files in a directory (and all subdirectories) quite easily
with a shell alias like the following:

```
alias sha512sum-all='find . '\''!'\'' -name SHA512SUM -type f -exec sha512sum {} + >> SHA512SUM'
```

However, updating this SHA512SUM file is not particularly efficient if we'd
like to avoid rehashing every file in the directory tree. Writing a script to
add and remove entries is relatively easy, but it would also be nice to update
the hashes for files that have changed. Using a plain SHA512SUM file makes it
quite difficult to identify files that have been modified, so this script
stores the metadata of the file when it is hashed, along with the hash, in a 
JSON file. This script can then also export a SHA512SUM file for transparent
operation to end users.


Usage
=====

The basic invocation of the script is of the form

```
hash_db.py -d PATH [global options] command [command-specific options]
```

* `--data-dir` or `-d`

  The path to the directory and sub-directories you wish to hash. This is 
  a required parameter.

Global Options
--------------

* `--pretend` or `-n`

  Omits writing the (new or modified) database to disk. Simulation/dry-run.
  
* `--verbose` or `-v`

  init & update function will only print lists of Added, Removed, and 
  Modified files if this switch is specified.
  
* `--jsondb` or `-j`

  Used to specify name of JSON file database, defaults to `hash_db.json`

Commands
--------

* `init`

  Creates a hash database in the current directory. Walks the directory tree
  and adds all files to the database. After completion, prints the list of
  added files.

* `update`

  Reads the hash database into memory and walks the directory tree to find any
  noteworthy files. This includes files that are not included in the hash
  database, files that have been removed since the last update, and files with
  a size or modification time that don't match the recorded values. Entries in
  the database are added, updated or modified as appropriate, and the new
  database is written to disk.

* `status`

  Reports added, modified, and removed files without performing any file
  hashing.

  Note that certain filesystems (vfat in particular) seem to report
  spurious mtime changes, and `status` necessarily will report such files.
  `hash_db.py --pretend update` can be used to filter these false positives at
  the cost of hashing each apparently-modified file.

* `verify`

  Reads the hash database into memory and hashes each file on disk. Reports
  each hash mismatch or file removal.

  Options:

  * `--verbose-failures`

    If hash verification fails, print filenames as soon as they are known in
    addition to the post-hashing summary.

  * `--update-mtimes`

    If hash verification of a file succeeds, update its stored modification
    time to match that of the file on disk.

* `import`

  Initializes a hash database from external hash files. Recognizes the
  following file patterns:

  * `hash_db.json`
  * `DIGESTS`
  * `DIGESTS.asc`
  * `SHA512SUM`
  * `SHA512SUM.asc`
  * `*.sha512sum`
  * `*.sha512sum.asc`

  Finds all hash files matching those patterns, and reads the contents of each
  into a single hash database. The size and modification time of each file in
  the hash database is read from disk, but the saved hashes are used as-is.

* `split`

  Required argument: `subdir`.

  Reads the hash database into memory, identifies entries that are contained in
  `subdir`, and writes the reduced hash database to `subdir/hash_db.json` with
  relative paths.

* `export`

  Writes hash entries to a `SHA512SUM` file in the same directory as
  `hash_db.json`.


Requirements
============

Python 3.4 or newer.


Open Issues/TODOs
=================

* Can not specify path to JSON database file (json file will remain relative to 
  -d PATH provided... so just let it fail if user selects wrong path/hashdb? or 
  mark in hashdb what the rel path is? Then could just load hashdb and get 
  -d PATH from there... let that be v3 of db? and fall back to base/v1 hashdb 
  in root of relative dir path otherwise?
* Add switch to specify hash function (vs current hardcoding). Also check that 
  the hash function matches hashes in provided database
* Ignore exported hashsum files (e.g. `SHA512SUM`)
* Importing a hasdb file seems to be redundant? look at again when not tired.
* If still paranoid, introduce partial hash verification for balance between 
  rsync style file change monitoring and expensive rehashing of all files, I 
  lost the repo to code that hashses first x bytes and last x bytes of a file.
* During the `verify` operation, it would be nice to pretty-print the number of
  bytes hashed instead of or in addition to the number of files.
* Are we cpu limited, or disk i/o? Test and then maybe introduce multithreading 
  code?
* As mentioned below, [mruffalo's](https://github.com/mruffalo/hash-db) main 
  motivation for writing this script was identifying the extent of filesystem 
  corruption. It's easy to find what's missing after an `fsck`, but it would 
  be much more helpful to hash everything that was dumped into `lost+found` 
  to put these files back where they belong.
  
  
Motivation
==========
Long ago, my home file server had a Linux software RAID 5 array of four 2TB
Seagate Barracuda drives. I've had a great deal of trouble with these drives --
between personal drives, an array that I was in charge of at my old university,
and an array at an old side job, I've experienced **17** drive failures. I've
experienced problems with 1TB, 2TB, and 3TB Barracudas.

Every one of these drive failures has manifested as unreadable sectors -- the
drives still powered on and identified themselves to the OS correctly. I found
the first such failure while copying some data from my file server to an
external hard drive, and the Linux `md` code dutifully removed the drive from
the array. At this point, my priority #1 became "back up all data before doing
anything else", so I reorganized some data into directories that more-or-less
matched the sizes of the various old hard drives that I could use for backups.
While `rsync`ing to these various drives, I found that **two** of the other
drives also had unreadable sectors that I wasn't aware of.

Since no two drives were unreadable in the same place, I figured I could force
the array online with different sets of three drives and copy whatever subset
of data was readable with those three. I didn't realize how remarkably stupid
this was until I tried it. Since the failed drive wasn't present when I moved
some data to different directories, the ext4 filesystem wasn't in a consistent
state. I hadn't actually changed the contents of any files while moving things
around, so file content was okay as far as I could tell. The corruption was
limited to filesystem metadata, so I had to manually figure out which
directories had some of their contents dumped into `lost+found` after a `fsck`.

This script is useful for keeping an up-to-date manifest of a directory tree
along with SHA512 hashes. Finding the extent of filesystem corruption is as
easy as `hash_db.py verify`, provided that the hash database is current.

After writing this, I realized that it would also be very useful for the USB
drive that I use to store various diagnostic/malware removal utilities. This
script can be used to verify that no extra files have been added to the drive
and that no EXE files have been tampered with.

<!---
# vim: set tw=79:
-->
