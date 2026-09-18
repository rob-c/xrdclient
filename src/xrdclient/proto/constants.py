"""XRootD wire constants.

Names are verbatim from the protocol vocabulary (nginx-xrootd
``src/protocols/root/protocol/opcodes.h`` + ``flags.h``) so every value is
greppable against the C reference. ``kXR_*`` names live only in this package.
"""

from __future__ import annotations

# ---- request opcodes (ClientRequestHdr.requestid) ----
kXR_auth = 3000
kXR_query = 3001
kXR_chmod = 3002
kXR_close = 3003
kXR_dirlist = 3004
kXR_gpfile = 3005
kXR_protocol = 3006
kXR_login = 3007
kXR_mkdir = 3008
kXR_mv = 3009
kXR_open = 3010
kXR_ping = 3011
kXR_chkpoint = 3012
kXR_read = 3013
kXR_rm = 3014
kXR_rmdir = 3015
kXR_sync = 3016
kXR_stat = 3017
kXR_set = 3018
kXR_write = 3019
kXR_fattr = 3020
kXR_prepare = 3021
kXR_statx = 3022
kXR_endsess = 3023
kXR_bind = 3024
kXR_readv = 3025
kXR_pgwrite = 3026
kXR_locate = 3027
kXR_truncate = 3028
kXR_sigver = 3029
kXR_pgread = 3030
kXR_writev = 3031
#: One past ``kXR_REQFENCE``: XProtocol.hh stops at ``kXR_writev``, and the
#: nginx-xrootd family took 3032 for a server-side range copy. A stock xrootd
#: (5.9 is the newest) answers "Invalid request code"; see ``File.clone``.
kXR_clone = 3032

kXR_1stRequest = 3000

# ---- vendor extensions ----
# Not in XProtocol.hh: the 3500 block is what XRootD.jl and XrdRust agreed on
# for the link operations a POSIX namespace has and XRootD never grew. A stock
# server answers kXR_Unsupported, which is what the client reports.
kXR_setattr = 3500
kXR_symlink = 3501
kXR_readlink = 3502
kXR_link = 3503

# ``kXR_setattr`` selectors: which halves of the 44-byte attribute block the
# server should apply. Mode is deliberately absent - kXR_chmod already carries
# it - and an id of -1 leaves that id alone, as ``chown(2)`` has always done.
kXR_sa_times = 0x01
kXR_sa_owner = 0x02

# ``utimensat(2)``'s two magic nanosecond values, which the one server
# implementing kXR_setattr passes straight through: "now" is what ``os.utime``
# means by no times at all, and "omit" is how one of a pair is left alone.
UTIME_NOW = (1 << 30) - 1
UTIME_OMIT = (1 << 30) - 2

# ---- response status (ServerResponseHdr.status) ----
kXR_ok = 0
kXR_oksofar = 4000
kXR_attn = 4001
kXR_authmore = 4002
kXR_error = 4003
kXR_redirect = 4004
kXR_wait = 4005
kXR_waitresp = 4006
kXR_status = 4007

# ---- kXR_attn action codes ----
kXR_asyncab = 5000
kXR_asyncdi = 5001
kXR_asyncms = 5002
kXR_asyncrd = 5003
kXR_asyncwt = 5004
kXR_asyncav = 5005
kXR_asynunav = 5006
kXR_asyncgo = 5007
kXR_asynresp = 5008

# ---- kXR_protocol response flags ----
kXR_isManager = 0x00000002
kXR_isServer = 0x00000001
kXR_attrCache = 0x00000080
kXR_attrMeta = 0x00000100
kXR_attrProxy = 0x00000200
kXR_attrSuper = 0x00000400
kXR_attrVirtRdr = 0x00000800
# Set on a redirect rather than announced by the server it came from.
kXR_recoverWrts = 0x00001000
kXR_collapseRedir = 0x00002000
kXR_ecRedir = 0x00004000
# What the server says it supports.
kXR_supposc = 0x00100000
kXR_suppgrw = 0x00200000
kXR_supgpf = 0x00400000
kXR_anongpf = 0x00800000
kXR_haveTLS = 0x80000000
kXR_gotoTLS = 0x40000000
kXR_tlsAny = 0x1F000000
kXR_tlsData = 0x01000000
kXR_tlsGPF = 0x02000000
kXR_tlsLogin = 0x04000000
kXR_tlsSess = 0x08000000
kXR_tlsTPC = 0x10000000
kXR_tlsGPFA = 0x20000000

# ---- handshake / kXR_protocol request ----
ROOTD_PQ = 2012
kXR_PROTOCOLVERSION = 0x00000520
kXR_secreqs = 0x01
kXR_ableTLS = 0x02
kXR_wantTLS = 0x04
kXR_ExpLogin = 0x03

# ---- kXR_login capver ----
kXR_asyncap = 0x80
kXR_ver005 = 0x05
SESSION_ID_LEN = 16

# ---- kXR_dirlist options ----
kXR_online = 0x01
kXR_dstat = 0x02
kXR_dcksm = 0x04

# ---- kXR_stat options ----
kXR_vfs = 0x01
# Another vendor bit: XProtocol.hh's stat options stop at kXR_vfs, and 0x40 is
# what nginx-xrootd reads for "describe the link, not its target". A server
# without it ignores the bit and follows the link, as it always did.
kXR_statNoFollow = 0x40

# ---- kXR_open options (u16) ----
kXR_compress = 0x0001
kXR_delete = 0x0002
kXR_force = 0x0004
kXR_new = 0x0008
kXR_open_read = 0x0010
kXR_open_updt = 0x0020
kXR_async = 0x0040
kXR_refresh = 0x0080
kXR_mkpath = 0x0100
kXR_open_apnd = 0x0200
kXR_retstat = 0x0400
kXR_replica = 0x0800
kXR_posc = 0x1000
kXR_nowait = 0x2000
kXR_seqio = 0x4000
kXR_open_wrto = 0x8000

# ---- kXR_mkdir options byte ----
kXR_mkdirpath = 0x01

# ---- kXR_query infotype ----
kXR_QStats = 1
kXR_QPrep = 2
kXR_Qcksum = 3
kXR_Qxattr = 4
kXR_Qspace = 5
kXR_Qckscan = 6
kXR_Qconfig = 7
kXR_Qvisa = 8
kXR_Qopaque = 16
kXR_Qopaquf = 32
kXR_Qopaqug = 64

# ---- kXR_prepare options ----
kXR_cancel = 1
kXR_notify = 2
kXR_noerrs = 4
kXR_stage = 8
kXR_wmode = 16
kXR_coloc = 32
kXR_fresh = 64
kXR_usetcp = 128

# ``kXR_evict`` is not one of those: it lives in ``optionX``, the extended
# half-word that came later, and 128 in the options byte is ``kXR_usetcp``.
# Sending it as a byte flag asks a server to stage over TCP, which is not
# what anyone means by "evict".
kXR_evict = 0x0001

# ---- kXR_locate options ----
kXR_addPeers = 0x0001
kXR_refreshLoc = 0x0080
kXR_prefname = 0x0100
kXR_4dirlist = 0x0400
kXR_nowaitLoc = 0x2000

# ---- kXR_fattr subcodes and options ----
kXR_fattrDel = 0
kXR_fattrGet = 1
kXR_fattrList = 2
kXR_fattrSet = 3
kXR_fattrMaxVars = 16
kXR_fattrIsNew = 0x01
kXR_fattrAData = 0x10
# nginx-xrootd's ``kXR_fa_recurse``: list the attributes of every file under a
# directory rather than the directory's own. The reply is not the reply the
# other subcodes send - see ``parse_fattr_tree``.
kXR_fattrRecurse = 0x20

# ---- kXR_chkpoint subcodes ----
kXR_ckpBegin = 0
kXR_ckpCommit = 1
kXR_ckpQuery = 2
kXR_ckpRollback = 3
kXR_ckpXeq = 4

# ---- kXR_writev / kXR_readv ----
kXR_wv_doSync = 0x01
READ_LIST_ENTRY_LEN = 16

# ---- kXR_clone ----
#: One ``clone_item``: ``src_fhandle[4] reserved[4] offset[8] len[8] dest[8]``.
CLONE_ITEM_LEN = 32

# ---- kXR_sigver ----
kXR_SHA256_sig = 0x01
kXR_nodata_sig = 0x01
#: ``secopt`` bit in the protocol security block: a write's payload joins its
#: signature instead of travelling unsigned.
kXR_secOData = 0x01
kXR_secNone = 0
kXR_secCompatible = 1
kXR_secStandard = 2
kXR_secIntense = 3
kXR_secPedantic = 4

# ---- paged I/O ----
kXR_pgPageSZ = 4096
kXR_pgUnitSZ = kXR_pgPageSZ + 4
kXR_pgMaxEpr = 128
kXR_pgRetry = 0x01
kXR_pgValid = 0x02
#: ``cseCRC[4] dlFirst[2] dlLast[2]`` in front of a ``kXR_pgwrite`` reply's
#: list of corrupt-page offsets.
PGW_CSE_HDRLEN = 8
#: How many times one page may be retransmitted before the write is a loss.
PGW_MAX_RETRY = 3
kXR_FinalResult = 0x00
kXR_PartialResult = 0x01
#: crc32c[4] streamID[2] requestid[1] resptype[1] reserved[4] dlen[4]
STATUS_HDR_LEN = 16
#: ...plus the 8-byte offset a paged-I/O response appends.
STATUS_BODY_LEN = 24

# ---- stat flags bitfield ----
kXR_xset = 0x01
kXR_isDir = 0x02
kXR_other = 0x04
kXR_offline = 0x08
kXR_readable = 0x10
kXR_writable = 0x20
kXR_poscpend = 0x40
kXR_bkpexist = 0x80

# ---- frame header lengths ----
DEFAULT_PORT = 1094
REQUEST_HDRLEN = 24
RESPONSE_HDRLEN = 8
FHANDLE_LEN = 4
NULL_FHANDLE = b"\x00\x00\x00\x00"

# Largest single read/write the protocol will carry in one frame.
MAX_FRAME_PAYLOAD = 0x7FFFFFFF

# Largest response body this client will buffer. Not a protocol limit: no
# legal reply comes close, because every reply is bounded by what was asked
# for. It is a fail-closed guard, so that a hostile - or merely
# desynchronised - server cannot make the client sit there accumulating a
# body it declared to be gigabytes long.
MAX_RESPONSE_BODY = 1 << 30

_REQUEST_NAMES: dict[int, str] = {
    v: k
    for k, v in list(globals().items())
    if k.startswith("kXR_") and (3000 <= v <= 3032 or 3501 <= v <= 3503)
}
_STATUS_NAMES: dict[int, str] = {
    kXR_ok: "kXR_ok",
    kXR_oksofar: "kXR_oksofar",
    kXR_attn: "kXR_attn",
    kXR_authmore: "kXR_authmore",
    kXR_error: "kXR_error",
    kXR_redirect: "kXR_redirect",
    kXR_wait: "kXR_wait",
    kXR_waitresp: "kXR_waitresp",
    kXR_status: "kXR_status",
}


def request_name(rid: int) -> str:
    """Protocol name of a request opcode (``3017`` -> ``"kXR_stat"``)."""
    return _REQUEST_NAMES.get(rid, f"kXR_unknown({rid})")


def status_name(status: int) -> str:
    """Protocol name of a response status code."""
    return _STATUS_NAMES.get(status, f"kXR_unknown({status})")
