#! /usr/bin/env python
"""
Save and load MinHash sketches in a JSON format, along with some metadata.
"""
from __future__ import print_function
import sys
import yaml
import hashlib
import sourmash_lib
from . import signature_json
from .logging import notify, error

import io
import gzip
import bz2file

SIGNATURE_VERSION=0.4


class FakeHLL(object):
    def __init__(self, cardinality):
        self.cardinality = int(cardinality)

    def estimate_cardinality(self):
        return self.cardinality

    def consume_string(self):
        raise Exception("cannot add to this HLL")

    def __eq__(self, other):
        return self.cardinality == other.cardinality


class SourmashSignature(object):
    "Main class for signature information."

    def __init__(self, email, minhash, name='', filename=''):
        self.d = {}
        self.d['class'] = 'sourmash_signature'
        self.d['type'] = 'mrnaseq'
        self.d['email'] = email
        if name:
            self.d['name'] = name
        if filename:
            self.d['filename'] = filename

        self.minhash = minhash

    def md5sum(self):
        "Calculate md5 hash of the bottom sketch, specifically."
        m = hashlib.md5()
        m.update(str(self.minhash.ksize).encode('ascii'))
        for k in self.minhash.get_mins():
            m.update(str(k).encode('utf-8'))
        return m.hexdigest()

    def __eq__(self, other):
        for k in self.d:
            if self.d[k] != other.d[k]:
                return False
            
        return self.minhash == other.minhash

    def name(self):
        "Return as nice a name as possible, defaulting to md5 prefix."
        if 'name' in self.d:
            return self.d.get('name')
        elif 'filename' in self.d:
            return self.d.get('filename')
        else:
            return self.md5sum()[:8]

    def _save(self):
        "Return metadata and a dictionary containing the sketch info."
        e = dict(self.d)
        minhash = self.minhash

        sketch = {}
        sketch['ksize'] = int(minhash.ksize)
        sketch['num'] = len(minhash)
        sketch['max_hash'] = int(minhash.max_hash)
        sketch['seed'] = int(minhash.seed)
        if self.minhash.track_abundance:
            values = minhash.get_mins(with_abundance=True)
            sketch['mins'] = list(map(int, values.keys()))
            sketch['abundances'] = list(map(int, values.values()))
        else:
            sketch['mins'] = list(map(int, minhash.get_mins()))
        sketch['md5sum'] = self.md5sum()

        if minhash.is_protein:
            sketch['molecule'] = 'protein'
        else:
            sketch['molecule'] = 'dna'

        if minhash.hll is not None:
            sketch['cardinality'] = minhash.hll.estimate_cardinality()

        e['signature'] = sketch

        return self.d.get('email'), self.d.get('name'), \
            self.d.get('filename'), sketch

    def similarity(self, other, ignore_abundance=False, downsample=False):
        "Compute similarity with the other MinHash signature."
        try:
            return self.minhash.similarity(other.minhash, ignore_abundance)
        except ValueError as e:
            if 'mismatch in max_hash' in str(e) and downsample:
                xx = self.minhash.downsample_max_hash(other.minhash)
                yy = other.minhash.downsample_max_hash(self.minhash)
                return xx.similarity(yy, ignore_abundance)
            else:
                raise

    def jaccard(self, other):
        "Compute Jaccard similarity with the other MinHash signature."
        return self.minhash.similarity(other.minhash, True)

    def containment(self, other):
        "Compute containment by the other signature. Note: ignores abundance."
        return self.minhash.containment(other.minhash)


def _guess_open(filename):
    """
    Make a best-effort guess as to how to parse the given sequence file.

    Handles '-' as shortcut for stdin.
    Deals with .gz and .bz2 as well as plain text.
    """
    magic_dict = {
        b"\x1f\x8b\x08": "gz",
        b"\x42\x5a\x68": "bz2",
    }  # Inspired by http://stackoverflow.com/a/13044946/1585509

    if filename == '-':
        filename = '/dev/stdin'

    bufferedfile = io.open(file=filename, mode='rb', buffering=8192)
    num_bytes_to_peek = max(len(x) for x in magic_dict)
    file_start = bufferedfile.peek(num_bytes_to_peek)
    compression = None
    for magic, ftype in magic_dict.items():
        if file_start.startswith(magic):
            compression = ftype
            break
    if compression is 'bz2':
        sigfile = bz2file.BZ2File(filename=bufferedfile)
    elif compression is 'gz':
        if not bufferedfile.seekable():
            bufferedfile.close()
            raise ValueError("gziped data not streamable, pipe through zcat \
                            first")
        sigfile = gzip.GzipFile(filename=filename)
    else:
        sigfile = bufferedfile

    return sigfile


def load_signatures(data, select_ksize=None, select_moltype=None,
                    ignore_md5sum=False):
    """Load a JSON string with signatures into classes.

    Returns list of SourmashSignature objects.

    Note, the order is not necessarily the same as what is in the source file.
    """
    if not data:
        return

    is_fp = False
    if hasattr(data, 'find') and data.find('sourmash_signature') == -1:   # filename
        try:                                  # is it a file handle?
            data.read
            is_fp = True
        except AttributeError:                # no - treat it like a filename.
            data = _guess_open(data)
            is_fp = True

    try:
        # support YAML until next major release
        if hasattr(data, 'peek'):
            p = data.peek(6)

            if p[:6] == b'class:': # YAML - legacy format
                for sig in yaml_load(data,
                                     select_ksize=select_ksize,
                                     select_moltype=select_moltype,
                                     ignore_md5sum=ignore_md5sum):
                    yield sig
                return

        # JSON format
        for sig in signature_json.load_signatures_json(data,
                                                     ignore_md5sum=ignore_md5sum):
            if not select_ksize or select_ksize == sig.minhash.ksize:
                if not select_moltype or \
                     sig.minhash.is_molecule_type(select_moltype):
                    yield sig
    except Exception as e:
        error("Error in parsing signature; quitting.")
        error("Exception: {}", str(e))
    finally:
        if is_fp:
            data.close()


def load_one_signature(data, select_ksize=None, select_moltype=None,
                       ignore_md5sum=False):
    sigiter = load_signatures(data, select_ksize=select_ksize,
                              select_moltype=select_moltype,
                              ignore_md5sum=ignore_md5sum)

    try:
        first_sig = next(sigiter)
    except StopIteration:
        raise ValueError("no signatures to load")

    try:
        next_sig = next(sigiter)
    except StopIteration:
        return first_sig

    raise ValueError("expected to load exactly one signature")


def save_signatures(siglist, fp=None):
    "Save multiple signatures into a JSON string (or into file handle 'fp')"
    return signature_json.save_signatures_json(siglist, fp)


def yaml_load(data, select_ksize=None, select_moltype=None,
              ignore_md5sum=False):
    # record header
    x = yaml.load_all(data)
    siglist = []
    for n, d in enumerate(x): # allow empty records & concat of signatures
        if n > 0 and n % 100 == 0:
           notify('...sig loading {}', n)
        if not d:
            continue
        if d.get('class') != 'sourmash_signature':
            raise Exception("incorrect class: %s" % d.get('class'))
        email = d['email']

        name = ''
        if 'name' in d:
            name = d['name']

        filename = ''
        if 'filename' in d:
            filename = d['filename']

        if 'signatures' not in d:
            raise Exception("invalid format")

        if d['version'] != SIGNATURE_VERSION:
            raise Exception("cannot load version %s" % (d['version']))

        for sketch in d['signatures']:
            sig = _load_one_signature(sketch, email, name, filename,
                                          ignore_md5sum)
            if not select_ksize or select_ksize == sig.minhash.ksize:
                if not select_moltype or \
                     sig.minhash.is_molecule_type(select_moltype):
                    yield sig


def _load_one_signature(sketch, email, name, filename, ignore_md5sum=False):
    """Helper function to unpack and check one signature block only."""
    ksize = sketch['ksize']
    mins = list(map(int, sketch['mins']))
    n = int(sketch['num'])
    molecule = sketch.get('molecule', 'dna')
    seed = sketch.get('seed', sourmash_lib.DEFAULT_SEED)
    if molecule == 'protein':
        is_protein = True
    elif molecule == 'dna':
        is_protein = False
    else:
        raise Exception("unknown molecule type: {}".format(molecule))

    max_hash = int(sketch.get('max_hash', 0))
    seed = int(sketch.get('seed', sourmash_lib.DEFAULT_SEED))

    track_abundance = 'abundances' in sketch
    e = sourmash_lib.MinHash(ksize=ksize, n=n,
                                is_protein=is_protein,
                                track_abundance=track_abundance,
                                max_hash=max_hash, seed=seed)
    if track_abundance:
        abundances = list(map(int, sketch['abundances']))
        e.set_abundances(dict(zip(mins, abundances)))
    else:
        for m in mins:
            e.add_hash(m)

    if 'cardinality' in sketch:
        e.hll = FakeHLL(int(sketch['cardinality']))

    sig = SourmashSignature(email, e)

    if not ignore_md5sum:
        md5sum = sketch['md5sum']
        if md5sum != sig.md5sum():
            raise Exception('error loading - md5 of minhash does not match')

    if name:
        sig.d['name'] = name
    if filename:
        sig.d['filename'] = filename

    return sig
