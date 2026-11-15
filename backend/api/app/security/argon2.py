"""Pure-Python Argon2 (RFC 9106).

Written because the locked stack names no password-hashing library. Correctness is
validated against the RFC 9106 §5 test vectors (tests/test_argon2.py) — the only
defensible basis for trusting a from-scratch cryptographic primitive.

Speed is deliberately not a goal: at OWASP-recommended parameters this reference
implementation is far too slow for production use, which is why the dependency
question was escalated instead of silently decided. See the WP-03 report.
"""

import hashlib
import struct

MASK64 = 0xFFFFFFFFFFFFFFFF
ARGON2D = 0
ARGON2I = 1
ARGON2ID = 2
VERSION_13 = 0x13
BLOCK_WORDS = 128
SYNC_POINTS = 4


def _rotr(x: int, n: int) -> int:
    return ((x >> n) | (x << (64 - n))) & MASK64


def _blamka(x: int, y: int) -> int:
    return (x + y + 2 * (x & 0xFFFFFFFF) * (y & 0xFFFFFFFF)) & MASK64


def _g(v: list[int], a: int, b: int, c: int, d: int) -> None:
    va, vb, vc, vd = v[a], v[b], v[c], v[d]
    va = _blamka(va, vb)
    vd = _rotr(vd ^ va, 32)
    vc = _blamka(vc, vd)
    vb = _rotr(vb ^ vc, 24)
    va = _blamka(va, vb)
    vd = _rotr(vd ^ va, 16)
    vc = _blamka(vc, vd)
    vb = _rotr(vb ^ vc, 63)
    v[a], v[b], v[c], v[d] = va, vb, vc, vd


def _round(v: list[int], idx: tuple[int, ...]) -> None:
    _g(v, idx[0], idx[4], idx[8], idx[12])
    _g(v, idx[1], idx[5], idx[9], idx[13])
    _g(v, idx[2], idx[6], idx[10], idx[14])
    _g(v, idx[3], idx[7], idx[11], idx[15])
    _g(v, idx[0], idx[5], idx[10], idx[15])
    _g(v, idx[1], idx[6], idx[11], idx[12])
    _g(v, idx[2], idx[7], idx[8], idx[13])
    _g(v, idx[3], idx[4], idx[9], idx[14])


_ROW_GROUPS = tuple(tuple(16 * i + k for k in range(16)) for i in range(8))
_COL_GROUPS = tuple(
    (
        2 * i, 2 * i + 1, 2 * i + 16, 2 * i + 17, 2 * i + 32, 2 * i + 33,
        2 * i + 48, 2 * i + 49, 2 * i + 64, 2 * i + 65, 2 * i + 80, 2 * i + 81,
        2 * i + 96, 2 * i + 97, 2 * i + 112, 2 * i + 113,
    )
    for i in range(8)
)


def _fill_block(
    prev: list[int], ref: list[int], curr: list[int] | None, with_xor: bool
) -> list[int]:
    r = [a ^ b for a, b in zip(ref, prev)]
    tmp = list(r)
    if with_xor and curr is not None:
        tmp = [a ^ b for a, b in zip(tmp, curr)]

    for group in _ROW_GROUPS:
        _round(r, group)
    for group in _COL_GROUPS:
        _round(r, group)

    return [a ^ b for a, b in zip(tmp, r)]


def _h_prime(data: bytes, outlen: int) -> bytes:
    """Variable-length hash H'^T (RFC 9106 §3.3)."""
    prefix = struct.pack("<I", outlen)
    if outlen <= 64:
        return hashlib.blake2b(prefix + data, digest_size=outlen).digest()

    out = bytearray()
    v = hashlib.blake2b(prefix + data, digest_size=64).digest()
    out += v[:32]
    remaining = outlen - 32
    while remaining > 64:
        v = hashlib.blake2b(v, digest_size=64).digest()
        out += v[:32]
        remaining -= 32
    out += hashlib.blake2b(v, digest_size=remaining).digest()
    return bytes(out)


def _le32(n: int) -> bytes:
    return struct.pack("<I", n)


def _initial_hash(
    password: bytes, salt: bytes, secret: bytes, associated: bytes,
    time_cost: int, memory_kib: int, parallelism: int, hash_len: int, kind: int,
) -> bytes:
    h = hashlib.blake2b(digest_size=64)
    h.update(_le32(parallelism))
    h.update(_le32(hash_len))
    h.update(_le32(memory_kib))
    h.update(_le32(time_cost))
    h.update(_le32(VERSION_13))
    h.update(_le32(kind))
    for blob in (password, salt, secret, associated):
        h.update(_le32(len(blob)))
        h.update(blob)
    return h.digest()


def _index_alpha(
    pass_no: int, slice_no: int, index: int, same_lane: bool,
    segment_length: int, lane_length: int, pseudo_rand: int,
) -> int:
    if pass_no == 0:
        if slice_no == 0:
            reference_area = index - 1
        elif same_lane:
            reference_area = slice_no * segment_length + index - 1
        else:
            reference_area = slice_no * segment_length + (-1 if index == 0 else 0)
    elif same_lane:
        reference_area = lane_length - segment_length + index - 1
    else:
        reference_area = lane_length - segment_length + (-1 if index == 0 else 0)

    rel = pseudo_rand & 0xFFFFFFFF
    rel = (rel * rel) >> 32
    rel = reference_area - 1 - ((reference_area * rel) >> 32)

    start = 0
    if pass_no != 0:
        start = 0 if slice_no == SYNC_POINTS - 1 else (slice_no + 1) * segment_length

    return (start + rel) % lane_length


def _next_addresses(input_block: list[int], zero_block: list[int]) -> list[int]:
    input_block[6] = (input_block[6] + 1) & MASK64
    first = _fill_block(zero_block, input_block, None, False)
    return _fill_block(zero_block, first, None, False)


def argon2(
    password: bytes,
    salt: bytes,
    *,
    time_cost: int = 3,
    memory_kib: int = 32,
    parallelism: int = 4,
    hash_len: int = 32,
    kind: int = ARGON2ID,
    secret: bytes = b"",
    associated: bytes = b"",
) -> bytes:
    if parallelism < 1:
        raise ValueError("parallelism must be >= 1")
    if hash_len < 4:
        raise ValueError("hash_len must be >= 4")
    if time_cost < 1:
        raise ValueError("time_cost must be >= 1")

    memory_kib = max(memory_kib, 8 * parallelism)
    segment_length = memory_kib // (parallelism * SYNC_POINTS)
    memory_blocks = segment_length * parallelism * SYNC_POINTS
    lane_length = segment_length * SYNC_POINTS

    h0 = _initial_hash(
        password, salt, secret, associated,
        time_cost, memory_blocks, parallelism, hash_len, kind,
    )

    blocks: list[list[int] | None] = [None] * memory_blocks
    for lane in range(parallelism):
        for idx in (0, 1):
            blob = _h_prime(h0 + _le32(idx) + _le32(lane), 1024)
            blocks[lane * lane_length + idx] = list(struct.unpack("<128Q", blob))

    zero_block = [0] * BLOCK_WORDS

    for pass_no in range(time_cost):
        for slice_no in range(SYNC_POINTS):
            for lane in range(parallelism):
                data_independent = kind == ARGON2I or (
                    kind == ARGON2ID and pass_no == 0 and slice_no < SYNC_POINTS // 2
                )
                input_block = [0] * BLOCK_WORDS
                address_block: list[int] | None = None
                if data_independent:
                    input_block[0] = pass_no
                    input_block[1] = lane
                    input_block[2] = slice_no
                    input_block[3] = memory_blocks
                    input_block[4] = time_cost
                    input_block[5] = kind

                starting_index = 0
                if pass_no == 0 and slice_no == 0:
                    starting_index = 2
                    if data_independent:
                        address_block = _next_addresses(input_block, zero_block)

                curr_offset = lane * lane_length + slice_no * segment_length + starting_index
                prev_offset = (
                    curr_offset + lane_length - 1
                    if curr_offset % lane_length == 0
                    else curr_offset - 1
                )

                for i in range(starting_index, segment_length):
                    # Crossing into a new lane resets "previous" to that lane's start.
                    if curr_offset % lane_length == 1:
                        prev_offset = curr_offset - 1
                    if data_independent:
                        if i % BLOCK_WORDS == 0:
                            address_block = _next_addresses(input_block, zero_block)
                        assert address_block is not None
                        pseudo_rand = address_block[i % BLOCK_WORDS]
                    else:
                        assert blocks[prev_offset] is not None
                        pseudo_rand = blocks[prev_offset][0]  # type: ignore[index]

                    ref_lane = (pseudo_rand >> 32) % parallelism
                    if pass_no == 0 and slice_no == 0:
                        ref_lane = lane

                    ref_index = _index_alpha(
                        pass_no, slice_no, i, ref_lane == lane,
                        segment_length, lane_length, pseudo_rand,
                    )
                    ref_block = blocks[ref_lane * lane_length + ref_index]
                    assert ref_block is not None
                    prev_block = blocks[prev_offset]
                    assert prev_block is not None
                    blocks[curr_offset] = _fill_block(
                        prev_block, ref_block, blocks[curr_offset],
                        with_xor=pass_no != 0,
                    )

                    curr_offset += 1
                    prev_offset += 1

    final = list(blocks[lane_length - 1])  # type: ignore[arg-type]
    for lane in range(1, parallelism):
        last = blocks[lane * lane_length + lane_length - 1]
        assert last is not None
        final = [a ^ b for a, b in zip(final, last)]

    return _h_prime(struct.pack("<128Q", *final), hash_len)
