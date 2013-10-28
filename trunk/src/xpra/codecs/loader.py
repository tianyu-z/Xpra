#!/usr/bin/env python
# This file is part of Xpra.
# Copyright (C) 2010-2013 Antoine Martin <antoine@devloop.org.uk>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

from xpra.log import Logger, debug_if_env
log = Logger()
debug = debug_if_env(log, "XPRA_CODEC_DEBUG")
error = log.error
warn = log.warn

codecs = {}
def codec_import_check(name, description, top_module, class_module, *classnames):
    debug("codec_import_check%s", (name, description, top_module, class_module, classnames))
    try:
        try:
            __import__(top_module, {}, {}, [])
            debug(" %s found, will check for %s in %s", top_module, classnames, class_module)
            for classname in classnames:
                ic =  __import__(class_module, {}, {}, classname)
                #warn("codec_import_check(%s, ..)=%s" % (name, ic))
                debug(" found %s: %s", name, ic)
                codecs[name] = ic
                return ic
        except ImportError, e:
            debug(" cannot import %s (%s): %s", name, description, e)
            #the required module does not exist
            debug(" xpra was probably built with the option: --without-%s", name)
    except Exception, e:
        warn("cannot load %s (%s): %s missing from %s: %s", name, description, classname, class_module, e)
    return None
codec_versions = {}
def add_codec_version(name, top_module, fieldname, invoke=False):
    try:
        module = __import__(top_module, {}, {}, [fieldname])
        if not hasattr(module, fieldname):
            warn("cannot find %s in %s", fieldname, module)
            return
        v = getattr(module, fieldname)
        if invoke and v:
            v = v()
        global codec_versions
        codec_versions[name] = v
    except ImportError, e:
        debug("cannot import %s: %s", name, e)
        #not present
        pass
    except Exception, e:
        warn("error during codec import: %s", e)


loaded = False
def load_codecs():
    global loaded
    if loaded:
        return
    loaded = True
    debug("loading codecs")
    codec_import_check("PIL", "Python Imaging Library", "PIL", "PIL", "Image")
    add_codec_version("PIL", "PIL.Image", "VERSION")
    
    codec_import_check("enc_vpx", "vpx encoder", "xpra.codecs.vpx", "xpra.codecs.vpx.encoder", "Encoder")
    codec_import_check("dec_vpx", "vpx decoder", "xpra.codecs.vpx", "xpra.codecs.vpx.decoder", "Decoder")
    add_codec_version("vpx", "xpra.codecs.vpx.encoder", "get_version", True)
    
    codec_import_check("enc_x264", "x264 encoder", "xpra.codecs.enc_x264", "xpra.codecs.enc_x264.encoder", "Encoder")
    add_codec_version("x264", "xpra.codecs.enc_x264.encoder", "get_version", True)
    
    codec_import_check("enc_nvenc", "nvenc encoder", "xpra.codecs.nvenc", "xpra.codecs.nvenc.encoder", "Encoder")
    add_codec_version("nvenc", "xpra.codecs.nvenc.encoder", "get_version", True)
    
    codec_import_check("csc_swscale", "swscale colorspace conversion", "xpra.codecs.csc_swscale", "xpra.codecs.csc_swscale.colorspace_converter", "ColorspaceConverter")
    add_codec_version("swscale", "xpra.codecs.csc_swscale.colorspace_converter", "get_version", True)
    
    codec_import_check("csc_opencl", "OpenCL colorspace conversion", "xpra.codecs.csc_opencl", "xpra.codecs.csc_opencl.colorspace_converter", "ColorspaceConverter")
    add_codec_version("opencl", "xpra.codecs.csc_opencl.colorspace_converter", "get_version", True)
    
    codec_import_check("csc_nvcuda", "CUDA colorspace conversion", "xpra.codecs.csc_nvcuda", "xpra.codecs.csc_nvcuda.colorspace_converter", "ColorspaceConverter")
    add_codec_version("nvcuda", "xpra.codecs.csc_nvcuda.colorspace_converter", "get_version", True)
    
    codec_import_check("dec_avcodec", "avcodec decoder", "xpra.codecs.dec_avcodec", "xpra.codecs.dec_avcodec.decoder", "Decoder")
    add_codec_version("avcodec", "xpra.codecs.dec_avcodec.decoder", "get_version", True)


    try:
        #python >=2.6 only and required for webp to work:
        bytearray()
        try:
            #these symbols are all available upstream as of libwebp 0.2:
            codec_import_check("enc_webp", "webp encoder", "xpra.codecs.webm", "xpra.codecs.webm.encode", "EncodeRGB", "EncodeRGBA", "EncodeBGR", "EncodeBGRA")
            codec_import_check("dec_webp", "webp encoder", "xpra.codecs.webm", "xpra.codecs.webm.decode", "DecodeRGB", "DecodeRGBA", "DecodeBGR", "DecodeBGRA")
            #these symbols were added in libwebp 0.4, and we added HAS_LOSSLESS to the wrapper:
            _enc_webp_lossless = codec_import_check("enc_webp_lossless", "webp encoder", "xpra.codecs.webm", "xpra.codecs.webm.encode", "HAS_LOSSLESS", "EncodeLosslessRGB", "EncodeLosslessRGBA", "EncodeLosslessBGRA", "EncodeLosslessBGR")
            if _enc_webp_lossless:
                #the fact that the python functions are defined is not enough
                #we need to check if the underlying C functions actually exist:
                if not _enc_webp_lossless.HAS_LOSSLESS:
                    del codecs["enc_webp_lossless"]
            add_codec_version("webp", "xpra.codecs.webm", "__VERSION__")
            webp_handlers = codec_import_check("webp_bitmap_handlers", "webp bitmap handler", "xpra.codecs.webm", "xpra.codecs.webm.handlers", "BitmapHandler")
            #we need the handlers to encode:
            if not webp_handlers:
                del codecs["enc_webp"]
                if "enc_webp_lossless" in codecs:
                    del codecs["enc_webp_lossless"]
        except Exception, e:
            warn("cannot load webp: " % e)
    except:
        #no bytearray, no webp
        pass
    debug("done loading codecs")
    debug("found:")
    #print("codec_status=%s" % codecs)
    for name in ALL_CODECS:
        debug("* %s : %s %s" % (name.ljust(20), str(name in codecs).ljust(10), codecs.get(name, "")))
    debug("codecs versions:")
    for name, version in codec_versions.items():
        debug("* %s : %s" % (name.ljust(20), version))


def get_codec(name):
    load_codecs()
    return codecs.get(name)

def has_codec(name):
    load_codecs()
    return name in codecs


ALL_CODECS = "PIL", "enc_vpx", "dec_vpx", "enc_x264", "enc_nvenc", "csc_swscale", "csc_opencl", "csc_nvcuda", "dec_avcodec", "enc_webp", "enc_webp_lossless", "webp_bitmap_handlers", "dec_webp"

PREFERED_ENCODING_ORDER = ["x264", "vpx", "webp", "png", "png/P", "png/L", "rgb", "jpeg"]

ENCODINGS_TO_NAME = {
                  "x264"    : "H.264",
                  "vpx"     : "VPx",
                  "png"     : "PNG (24/32bpp)",
                  "png/P"   : "PNG (8bpp colour)",
                  "png/L"   : "PNG (8bpp grayscale)",
                  "webp"    : "WebP",
                  "jpeg"    : "JPEG",
                  "rgb"     : "Raw RGB + zlib (24/32bpp)",
                }

ENCODINGS_HELP = {
                  "x264"    : "H.264 video codec",
                  "vpx"     : "VPx video codec",
                  "png"     : "Portable Network Graphics (24 or 32bpp for transparency)",
                  "png/P"   : "Portable Network Graphics (8bpp colour)",
                  "png/L"   : "Portable Network Graphics (8bpp grayscale)",
                  "webp"    : "WebP compression (lossless or lossy)",
                  "jpeg"    : "JPEG lossy compression",
                  "rgb"     : "Raw RGB pixels, lossless, compressed using zlib (24 or 32bpp for transparency)",
                  }

HELP_ORDER = ("x264", "vpx", "webp", "png", "png/P", "png/L", "rgb", "jpeg")

def encodings_help(encodings):
    h = []
    for e in HELP_ORDER:
        if e in encodings:
            ehelp = ENCODINGS_HELP.get(e)
            h.append(e.ljust(12) + ehelp)
    return h


def main():
    import sys
    import logging
    logging.basicConfig(format="%(message)s")
    logging.root.setLevel(logging.INFO)
    
    load_codecs()
    print("codecs/csc modules found:")
    #print("codec_status=%s" % codecs)
    for name in ALL_CODECS:
        print("* %s : %s %s" % (name.ljust(20), str(name in codecs).ljust(10), codecs.get(name, "")))
    print("")
    print("codecs versions:")
    for name, version in codec_versions.items():
        print("* %s : %s" % (name.ljust(20), version))

    if sys.platform.startswith("win"):
        print("\nPress Enter to close")
        sys.stdin.readline()


if __name__ == "__main__":
    main()