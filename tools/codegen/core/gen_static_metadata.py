#!/usr/bin/env python2.7

# Copyright 2015 gRPC authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import hashlib
import itertools
import collections
import os
import sys
import subprocess
import re
import perfection

# Configuration: a list of either strings or 2-tuples of strings.
# A single string represents a static grpc_mdstr.
# A 2-tuple represents a static grpc_mdelem (and appropriate grpc_mdstrs will
# also be created).
# The list of 2-tuples must begin with the static hpack table elements as
# defined by RFC 7541 and be in the same order because of an hpack encoding
# performance optimization that relies on this. If you want to change this, then
# you must change the implementation of the encoding optimization as well.

CONFIG = [
    # metadata strings
    'host',
    'grpc-timeout',
    'grpc-internal-encoding-request',
    'grpc-internal-stream-encoding-request',
    'grpc-payload-bin',
    ':path',
    'grpc-encoding',
    'grpc-accept-encoding',
    'user-agent',
    ':authority',
    'grpc-message',
    'grpc-status',
    'grpc-server-stats-bin',
    'grpc-tags-bin',
    'grpc-trace-bin',
    'grpc-previous-rpc-attempts',
    'grpc-retry-pushback-ms',
    '1',
    '2',
    '3',
    '4',
    '',
    # channel arg keys
    'grpc.wait_for_ready',
    'grpc.timeout',
    'grpc.max_request_message_bytes',
    'grpc.max_response_message_bytes',
    # well known method names
    '/grpc.lb.v1.LoadBalancer/BalanceLoad',
    '/grpc.health.v1.Health/Watch',
    '/envoy.service.discovery.v2.AggregatedDiscoveryService/StreamAggregatedResources',
    # compression algorithm names
    'deflate',
    'gzip',
    'stream/gzip',
    # metadata elements
    # begin hpack static elements
    (':authority', ''),
    (':method', 'GET'),
    (':method', 'POST'),
    (':path', '/'),
    (':path', '/index.html'),
    (':scheme', 'http'),
    (':scheme', 'https'),
    (':status', '200'),
    (':status', '204'),
    (':status', '206'),
    (':status', '304'),
    (':status', '400'),
    (':status', '404'),
    (':status', '500'),
    ('accept-charset', ''),
    ('accept-encoding', 'gzip, deflate'),
    ('accept-language', ''),
    ('accept-ranges', ''),
    ('accept', ''),
    ('access-control-allow-origin', ''),
    ('age', ''),
    ('allow', ''),
    ('authorization', ''),
    ('cache-control', ''),
    ('content-disposition', ''),
    ('content-encoding', ''),
    ('content-language', ''),
    ('content-length', ''),
    ('content-location', ''),
    ('content-range', ''),
    ('content-type', ''),
    ('cookie', ''),
    ('date', ''),
    ('etag', ''),
    ('expect', ''),
    ('expires', ''),
    ('from', ''),
    ('host', ''),
    ('if-match', ''),
    ('if-modified-since', ''),
    ('if-none-match', ''),
    ('if-range', ''),
    ('if-unmodified-since', ''),
    ('last-modified', ''),
    ('link', ''),
    ('location', ''),
    ('max-forwards', ''),
    ('proxy-authenticate', ''),
    ('proxy-authorization', ''),
    ('range', ''),
    ('referer', ''),
    ('refresh', ''),
    ('retry-after', ''),
    ('server', ''),
    ('set-cookie', ''),
    ('strict-transport-security', ''),
    ('transfer-encoding', ''),
    ('user-agent', ''),
    ('vary', ''),
    ('via', ''),
    ('www-authenticate', ''),
    # end hpack static elements
    ('grpc-status', '0'),
    ('grpc-status', '1'),
    ('grpc-status', '2'),
    ('grpc-encoding', 'identity'),
    ('grpc-encoding', 'gzip'),
    ('grpc-encoding', 'deflate'),
    ('te', 'trailers'),
    ('content-type', 'application/grpc'),
    (':scheme', 'grpc'),
    (':method', 'PUT'),
    ('accept-encoding', ''),
    ('content-encoding', 'identity'),
    ('content-encoding', 'gzip'),
    ('lb-token', ''),
    ('lb-cost-bin', ''),
]

# All entries here are ignored when counting non-default initial metadata that
# prevents the chttp2 server from sending a Trailers-Only response.
METADATA_BATCH_CALLOUTS = [
    # (name)
    (':path'),
    (':method'),
    (':status'),
    (':authority'),
    (':scheme'),
    ('te'),
    ('grpc-message'),
    ('grpc-status'),
    ('grpc-payload-bin'),
    ('grpc-encoding'),
    ('grpc-accept-encoding'),
    ('grpc-server-stats-bin'),
    ('grpc-tags-bin'),
    ('grpc-trace-bin'),
    ('content-type'),
    ('content-encoding'),
    ('accept-encoding'),
    ('grpc-internal-encoding-request'),
    ('grpc-internal-stream-encoding-request'),
    ('user-agent'),
    ('host'),
    ('lb-token'),
    ('grpc-previous-rpc-attempts'),
    ('grpc-retry-pushback-ms'),
]

COMPRESSION_ALGORITHMS = [
    'identity',
    'deflate',
    'gzip',
]

STREAM_COMPRESSION_ALGORITHMS = [
    'identity',
    'gzip',
]


# utility: mangle the name of a config
def mangle(elem, name=None):
    xl = {
        '-': '_',
        ':': '',
        '/': 'slash',
        '.': 'dot',
        ',': 'comma',
        ' ': '_',
    }

    def m0(x):
        if not x:
            return 'empty'
        r = ''
        for c in x:
            put = xl.get(c, c.lower())
            if not put:
                continue
            last_is_underscore = r[-1] == '_' if r else True
            if last_is_underscore and put == '_':
                continue
            elif len(put) > 1:
                if not last_is_underscore:
                    r += '_'
                r += put
                r += '_'
            else:
                r += put
        if r[-1] == '_':
            r = r[:-1]
        return r

    def n(default, name=name):
        if name is None:
            return 'grpc_%s_' % default
        if name == '':
            return ''
        return 'grpc_%s_' % name

    if isinstance(elem, tuple):
        return '%s%s_%s' % (n('mdelem'), m0(elem[0]), m0(elem[1]))
    else:
        return '%s%s' % (n('mdstr'), m0(elem))


# utility: generate some hash value for a string
def fake_hash(elem):
    return hashlib.md5(elem).hexdigest()[0:8]


# utility: print a big comment block into a set of files
def put_banner(files, banner):
    for f in files:
        print >> f, '/*'
        for line in banner:
            print >> f, ' * %s' % line
        print >> f, ' */'
        print >> f


# build a list of all the strings we need
all_strs = list()
all_elems = list()
static_userdata = {}
# put metadata batch callouts first, to make the check of if a static metadata
# string is a callout trivial
for elem in METADATA_BATCH_CALLOUTS:
    if elem not in all_strs:
        all_strs.append(elem)
for elem in CONFIG:
    if isinstance(elem, tuple):
        if elem[0] not in all_strs:
            all_strs.append(elem[0])
        if elem[1] not in all_strs:
            all_strs.append(elem[1])
        if elem not in all_elems:
            all_elems.append(elem)
    else:
        if elem not in all_strs:
            all_strs.append(elem)
compression_elems = []
for mask in range(1, 1 << len(COMPRESSION_ALGORITHMS)):
    val = ','.join(COMPRESSION_ALGORITHMS[alg]
                   for alg in range(0, len(COMPRESSION_ALGORITHMS))
                   if (1 << alg) & mask)
    elem = ('grpc-accept-encoding', val)
    if val not in all_strs:
        all_strs.append(val)
    if elem not in all_elems:
        all_elems.append(elem)
    compression_elems.append(elem)
    static_userdata[elem] = 1 + (mask | 1)
stream_compression_elems = []
for mask in range(1, 1 << len(STREAM_COMPRESSION_ALGORITHMS)):
    val = ','.join(STREAM_COMPRESSION_ALGORITHMS[alg]
                   for alg in range(0, len(STREAM_COMPRESSION_ALGORITHMS))
                   if (1 << alg) & mask)
    elem = ('accept-encoding', val)
    if val not in all_strs:
        all_strs.append(val)
    if elem not in all_elems:
        all_elems.append(elem)
    stream_compression_elems.append(elem)
    static_userdata[elem] = 1 + (mask | 1)

# output configuration
args = sys.argv[1:]
H = None
C = None
D = None
if args:
    if 'header' in args:
        H = sys.stdout
    else:
        H = open('/dev/null', 'w')
    if 'source' in args:
        C = sys.stdout
    else:
        C = open('/dev/null', 'w')
    if 'dictionary' in args:
        D = sys.stdout
    else:
        D = open('/dev/null', 'w')
else:
    H = open(
        os.path.join(
            os.path.dirname(sys.argv[0]),
            '../../../src/core/lib/transport/static_metadata.h'), 'w')
    C = open(
        os.path.join(
            os.path.dirname(sys.argv[0]),
            '../../../src/core/lib/transport/static_metadata.cc'), 'w')
    D = open(
        os.path.join(
            os.path.dirname(sys.argv[0]),
            '../../../test/core/end2end/fuzzers/hpack.dictionary'), 'w')

# copy-paste copyright notice from this file
with open(sys.argv[0]) as my_source:
    copyright = []
    for line in my_source:
        if line[0] != '#':
            break
    for line in my_source:
        if line[0] == '#':
            copyright.append(line)
            break
    for line in my_source:
        if line[0] != '#':
            break
        copyright.append(line)
    put_banner([H, C], [line[2:].rstrip() for line in copyright])

hex_bytes = [ord(c) for c in 'abcdefABCDEF0123456789']


def esc_dict(line):
    out = "\""
    for c in line:
        if 32 <= c < 127:
            if c != ord('"'):
                out += chr(c)
            else:
                out += "\\\""
        else:
            out += '\\x%02X' % c
    return out + "\""


put_banner([H, C], """WARNING: Auto-generated code.

To make changes to this file, change
tools/codegen/core/gen_static_metadata.py, and then re-run it.

See metadata.h for an explanation of the interface here, and metadata.cc for
an explanation of what's going on.
""".splitlines())

print >> H, '#ifndef GRPC_CORE_LIB_TRANSPORT_STATIC_METADATA_H'
print >> H, '#define GRPC_CORE_LIB_TRANSPORT_STATIC_METADATA_H'
print >> H
print >> H, '#include <grpc/support/port_platform.h>'
print >> H
print >> H, '#include <cstdint>'
print >> H
print >> H, '#include "src/core/lib/transport/metadata.h"'
print >> H
print >> C, '#include <grpc/support/port_platform.h>'
print >> C
print >> C, '#include "src/core/lib/transport/static_metadata.h"'
print >> C
print >> C, '#include "src/core/lib/slice/slice_internal.h"'
print >> C

str_ofs = 0
id2strofs = {}
for i, elem in enumerate(all_strs):
    id2strofs[i] = str_ofs
    str_ofs += len(elem)


def slice_def(i):
    return ('{&grpc_static_metadata_refcounts[%d],'
            ' {{%d, g_bytes+%d}}}') % (i, len(all_strs[i]), id2strofs[i])


# validate configuration
for elem in METADATA_BATCH_CALLOUTS:
    assert elem in all_strs

print >> H, '#define GRPC_STATIC_MDSTR_COUNT %d' % len(all_strs)
print >> H, ('extern const grpc_slice '
             'grpc_static_slice_table[GRPC_STATIC_MDSTR_COUNT];')
for i, elem in enumerate(all_strs):
    print >> H, '/* "%s" */' % elem
    print >> H, '#define %s (grpc_static_slice_table[%d])' % (
        mangle(elem).upper(), i)
print >> H
print >> C, 'static uint8_t g_bytes[] = {%s};' % (','.join(
    '%d' % ord(c) for c in ''.join(all_strs)))
print >> C
print >> C, ('static grpc_slice_refcount static_sub_refcnt;')
print >> H, ('extern grpc_slice_refcount '
             'grpc_static_metadata_refcounts[GRPC_STATIC_MDSTR_COUNT];')
print >> C, ('grpc_slice_refcount '
             'grpc_static_metadata_refcounts[GRPC_STATIC_MDSTR_COUNT] = {')
for i, elem in enumerate(all_strs):
    print >> C, ('  grpc_slice_refcount(&static_sub_refcnt, '
                 'grpc_slice_refcount::Type::STATIC), ')
print >> C, '};'
print >> C
print >> H, '#define GRPC_IS_STATIC_METADATA_STRING(slice) \\'
print >> H, ('  ((slice).refcount != NULL && (slice).refcount->GetType() == '
             'grpc_slice_refcount::Type::STATIC)')
print >> H
print >> C, ('const grpc_slice grpc_static_slice_table[GRPC_STATIC_MDSTR_COUNT]'
             ' = {')
for i, elem in enumerate(all_strs):
    print >> C, slice_def(i) + ','
print >> C, '};'
print >> C
print >> H, '#define GRPC_STATIC_METADATA_INDEX(static_slice) \\'
print >> H, ('  (static_cast<intptr_t>(((static_slice).refcount - '
             'grpc_static_metadata_refcounts)))')
print >> H

print >> D, '# hpack fuzzing dictionary'
for i, elem in enumerate(all_strs):
    print >> D, '%s' % (esc_dict([len(elem)] + [ord(c) for c in elem]))
for i, elem in enumerate(all_elems):
    print >> D, '%s' % (esc_dict([0, len(elem[0])] + [ord(c) for c in elem[0]] +
                                 [len(elem[1])] + [ord(c) for c in elem[1]]))

print >> H, '#define GRPC_STATIC_MDELEM_COUNT %d' % len(all_elems)
print >> H, ('extern grpc_core::StaticMetadata '
             'grpc_static_mdelem_table[GRPC_STATIC_MDELEM_COUNT];')
print >> H, ('extern uintptr_t '
             'grpc_static_mdelem_user_data[GRPC_STATIC_MDELEM_COUNT];')
print >> H, ('extern grpc_mdelem '
             'grpc_static_mdelem_manifested[GRPC_STATIC_MDELEM_COUNT];')
print >> C, ('''
/* Warning: the core static metadata currently operates under the soft constraint
that the first GRPC_CHTTP2_LAST_STATIC_ENTRY (61) entries must contain
metadata specified by the http2 hpack standard. The CHTTP2 transport reads the
core metadata with this assumption in mind. If the order of the core static
metadata is to be changed, then the CHTTP2 transport must be changed as well to
stop relying on the core metadata. */
''')
print >> C, ('grpc_mdelem '
             'grpc_static_mdelem_manifested[GRPC_STATIC_MDELEM_COUNT] = {')
print >> C, '// clang-format off'
static_mds = []
for i, elem in enumerate(all_elems):
    md_name = mangle(elem).upper()
    md_human_readable = '"%s": "%s"' % elem
    md_spec = '    /* %s: \n     %s */\n' % (md_name, md_human_readable)
    md_spec += '    GRPC_MAKE_MDELEM(\n'
    md_spec += (('        &grpc_static_mdelem_table[%d].data(),\n' % i) +
                '        GRPC_MDELEM_STORAGE_STATIC)')
    static_mds.append(md_spec)
print >> C, ',\n'.join(static_mds)
print >> C, '// clang-format on'
print >> C, ('};')

for i, elem in enumerate(all_elems):
    md_name = mangle(elem).upper()
    print >> H, '/* "%s": "%s" */' % elem
    print >> H, ('#define %s (grpc_static_mdelem_manifested[%d])' % (md_name,
                                                                     i))
print >> H

print >> C, ('uintptr_t grpc_static_mdelem_user_data[GRPC_STATIC_MDELEM_COUNT] '
             '= {')
print >> C, '  %s' % ','.join(
    '%d' % static_userdata.get(elem, 0) for elem in all_elems)
print >> C, '};'
print >> C


def str_idx(s):
    for i, s2 in enumerate(all_strs):
        if s == s2:
            return i


def md_idx(m):
    for i, m2 in enumerate(all_elems):
        if m == m2:
            return i


def offset_trials(mink):
    yield 0
    for i in range(1, 100):
        for mul in [-1, 1]:
            yield mul * i


def perfect_hash(keys, name):
    p = perfection.hash_parameters(keys)

    def f(i, p=p):
        i += p.offset
        x = i % p.t
        y = i / p.t
        return x + p.r[y]

    return {
        'PHASHRANGE': p.t - 1 + max(p.r),
        'PHASHNKEYS': len(p.slots),
        'pyfunc': f,
        'code': """
static const int8_t %(name)s_r[] = {%(r)s};
static uint32_t %(name)s_phash(uint32_t i) {
  i %(offset_sign)s= %(offset)d;
  uint32_t x = i %% %(t)d;
  uint32_t y = i / %(t)d;
  uint32_t h = x;
  if (y < GPR_ARRAY_SIZE(%(name)s_r)) {
    uint32_t delta = (uint32_t)%(name)s_r[y];
    h += delta;
  }
  return h;
}
    """ % {
            'name': name,
            'r': ','.join('%d' % (r if r is not None else 0) for r in p.r),
            't': p.t,
            'offset': abs(p.offset),
            'offset_sign': '+' if p.offset > 0 else '-'
        }
    }


elem_keys = [
    str_idx(elem[0]) * len(all_strs) + str_idx(elem[1]) for elem in all_elems
]
elem_hash = perfect_hash(elem_keys, 'elems')
print >> C, elem_hash['code']

keys = [0] * int(elem_hash['PHASHRANGE'])
idxs = [255] * int(elem_hash['PHASHNKEYS'])
for i, k in enumerate(elem_keys):
    h = elem_hash['pyfunc'](k)
    assert keys[h] == 0
    keys[h] = k
    idxs[h] = i
print >> C, 'static const uint16_t elem_keys[] = {%s};' % ','.join(
    '%d' % k for k in keys)
print >> C, 'static const uint8_t elem_idxs[] = {%s};' % ','.join(
    '%d' % i for i in idxs)
print >> C

print >> H, 'grpc_mdelem grpc_static_mdelem_for_static_strings(intptr_t a, intptr_t b);'
print >> C, 'grpc_mdelem grpc_static_mdelem_for_static_strings(intptr_t a, intptr_t b) {'
print >> C, '  if (a == -1 || b == -1) return GRPC_MDNULL;'
print >> C, '  uint32_t k = static_cast<uint32_t>(a * %d + b);' % len(all_strs)
print >> C, '  uint32_t h = elems_phash(k);'
print >> C, '  return h < GPR_ARRAY_SIZE(elem_keys) && elem_keys[h] == k && elem_idxs[h] != 255 ? GRPC_MAKE_MDELEM(&grpc_static_mdelem_table[elem_idxs[h]].data(), GRPC_MDELEM_STORAGE_STATIC) : GRPC_MDNULL;'
print >> C, '}'
print >> C

print >> C, 'grpc_core::StaticMetadata grpc_static_mdelem_table[GRPC_STATIC_MDELEM_COUNT] = {'
for idx, (a, b) in enumerate(all_elems):
    print >> C, 'grpc_core::StaticMetadata(%s,%s, %d),' % (
        slice_def(str_idx(a)), slice_def(str_idx(b)), idx)
print >> C, '};'

print >> H, 'typedef enum {'
for elem in METADATA_BATCH_CALLOUTS:
    print >> H, '  %s,' % mangle(elem, 'batch').upper()
print >> H, '  GRPC_BATCH_CALLOUTS_COUNT'
print >> H, '} grpc_metadata_batch_callouts_index;'
print >> H
print >> H, 'typedef union {'
print >> H, '  struct grpc_linked_mdelem *array[GRPC_BATCH_CALLOUTS_COUNT];'
print >> H, '  struct {'
for elem in METADATA_BATCH_CALLOUTS:
    print >> H, '  struct grpc_linked_mdelem *%s;' % mangle(elem, '').lower()
print >> H, '  } named;'
print >> H, '} grpc_metadata_batch_callouts;'
print >> H
print >> H, '#define GRPC_BATCH_INDEX_OF(slice) \\'
print >> H, '  (GRPC_IS_STATIC_METADATA_STRING((slice)) ? static_cast<grpc_metadata_batch_callouts_index>(GPR_CLAMP(GRPC_STATIC_METADATA_INDEX((slice)), 0, static_cast<intptr_t>(GRPC_BATCH_CALLOUTS_COUNT))) : GRPC_BATCH_CALLOUTS_COUNT)'
print >> H

print >> H, 'extern const uint8_t grpc_static_accept_encoding_metadata[%d];' % (
    1 << len(COMPRESSION_ALGORITHMS))
print >> C, 'const uint8_t grpc_static_accept_encoding_metadata[%d] = {' % (
    1 << len(COMPRESSION_ALGORITHMS))
print >> C, '0,%s' % ','.join('%d' % md_idx(elem) for elem in compression_elems)
print >> C, '};'
print >> C

print >> H, '#define GRPC_MDELEM_ACCEPT_ENCODING_FOR_ALGORITHMS(algs) (GRPC_MAKE_MDELEM(&grpc_static_mdelem_table[grpc_static_accept_encoding_metadata[(algs)]].data(), GRPC_MDELEM_STORAGE_STATIC))'
print >> H

print >> H, 'extern const uint8_t grpc_static_accept_stream_encoding_metadata[%d];' % (
    1 << len(STREAM_COMPRESSION_ALGORITHMS))
print >> C, 'const uint8_t grpc_static_accept_stream_encoding_metadata[%d] = {' % (
    1 << len(STREAM_COMPRESSION_ALGORITHMS))
print >> C, '0,%s' % ','.join(
    '%d' % md_idx(elem) for elem in stream_compression_elems)
print >> C, '};'

print >> H, '#define GRPC_MDELEM_ACCEPT_STREAM_ENCODING_FOR_ALGORITHMS(algs) (GRPC_MAKE_MDELEM(&grpc_static_mdelem_table[grpc_static_accept_stream_encoding_metadata[(algs)]].data(), GRPC_MDELEM_STORAGE_STATIC))'

print >> H, '#endif /* GRPC_CORE_LIB_TRANSPORT_STATIC_METADATA_H */'

H.close()
C.close()