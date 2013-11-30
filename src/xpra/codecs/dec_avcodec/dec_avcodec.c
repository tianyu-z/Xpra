/* This file is part of Xpra.
 * Copyright (C) 2012, 2013 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
 * Copyright (C) 2012, 2013 Antoine Martin <antoine@devloop.org.uk>
 * Xpra is released under the terms of the GNU GPL v2, or, at your option, any
 * later version. See the file COPYING for details.
 */

#include "dec_avcodec.h"
#include <libavcodec/avcodec.h>

const char *get_avcodec_version(void)
{
	return LIBAVCODEC_IDENT;
}