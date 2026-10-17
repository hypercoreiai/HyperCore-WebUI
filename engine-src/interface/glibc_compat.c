/*
 * src/interface/glibc_compat.c - keep the artifact's glibc floor inside the range
 * the deployment targets actually have.
 *
 * `sqrtf` is versioned GLIBC_2.43 in glibc 2.43 (this build host). Any reference
 * the linker resolves against that libm is stamped `sqrtf@GLIBC_2.43` into the
 * .so, and the stamp survives into the shipped artifact. On Ubuntu 24.04 (glibc
 * 2.39, the image in Dockerfile) and on Debian (glibc 2.41) that version does not
 * exist, so the first thing a customer sees is a link or load failure with no
 * source-level cause:
 *
 *   /usr/bin/ld: ./libhypercore_api.so: undefined reference to `sqrtf@GLIBC_2.43'
 *
 * The reference is not ours - it comes from the statically linked ggml archives.
 * -Wl,--wrap=sqrtf redirects every internal reference to __wrap_sqrtf, defined
 * here, so no libm symbol is ever pulled in for the square root. The compiler
 * lowers __builtin_sqrtf to a single SSE instruction, so this is the same code
 * the inlined call would have produced, just without the version stamp.
 *
 * Hidden visibility (C_VISIBILITY_PRESET hidden on the target) keeps __wrap_sqrtf
 * off the public surface; it is not part of the ABI contract.
 */
float __wrap_sqrtf(float value)
{
    return __builtin_sqrtf(value);
}
