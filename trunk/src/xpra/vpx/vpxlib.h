#ifdef _WIN32
#include "stdint.h"
#include "inttypes.h"
#else
#include "stdint.h"
#endif

/** Opaque structure - "context". You must have a context to encode images of a given size */
struct vpx_context;

/** Create an encoding context for images of a given size.  */
struct vpx_context *init_encoder(int width, int height);

/** Create a decoding context for images of a given size. */
struct vpx_context *init_decoder(int width, int height);

/** Cleanup encoding context. Must be freed after calling this function. */
void clean_encoder(struct vpx_context *ctx);

/** Cleanup decoding context. Must be freed after calling this function. */
void clean_decoder(struct vpx_context *ctx);

/** Compress an image using the given context. 
 @param in: Input buffer, format is packed RGB24.
 @param stride: Input stride (size is taken from context).
 @param out: Will be set to point to the output data. This output buffer MUST NOT BE FREED and will be erased on the 
 next call to compress_image.
 @param outsz: Output size
*/
int compress_image(struct vpx_context *ctx, uint8_t *in, int w, int h, int stride, uint8_t **out, int *outsz);

/** Decompress an image using the given context. 
 @param in: Input buffer, format is H264.
 @param size: Input size.
 @param out: Will be set to point to the output data in RGB24 format.
 @param outstride: Output stride.
*/
int decompress_image(struct vpx_context *ctx, uint8_t *in, int size, uint8_t **out, int *outsize, int *outstride);
