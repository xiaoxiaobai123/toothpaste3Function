/*
 * image_updater — watches an RGB565 file and pushes it to /dev/fb0.
 *
 * Companion to the Python vision binary: Python writes the composed
 * operator-screen image as RGB565 (with a 2x int32 width/height header),
 * inotify IN_CLOSE_WRITE wakes us, we render to the framebuffer.
 *
 * v0.3.8 architecture:
 *   * mmap()  source file (no malloc/read per frame, page cache friendly)
 *   * NEON    RGB565→ARGB conversion when src dims == fb dims (fast path)
 *   * scalar  scaling fallback when src and fb differ (compatible with
 *             pre-v0.3.8 Python that didn't pre-scale)
 *   * single-thread main loop (no pthread per frame)
 *   * black letterbox (was orange in v0.3.7)
 *   * one-line-per-minute heartbeat (introduced v0.3.4)
 *
 * Default watch path: /home/pi/output_image.rgb565
 * Override via argv[1] if you've moved it.
 */

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/ioctl.h>
#include <linux/fb.h>
#include <unistd.h>
#include <string.h>
#include <sys/inotify.h>
#include <time.h>

#if defined(__aarch64__) || defined(__ARM_NEON)
#include <arm_neon.h>
#define HAVE_NEON 1
#else
#define HAVE_NEON 0
#endif

#define EVENT_SIZE      (sizeof(struct inotify_event))
#define BUF_LEN         (1024 * (EVENT_SIZE + 16))
#define DEFAULT_PATH    "/home/pi/output_image.rgb565"
#define HEADER_BYTES    (2 * sizeof(int32_t))
#define LETTERBOX_ARGB  0xFF000000u  /* opaque black */

static double get_elapsed_time(struct timespec start, struct timespec end) {
    return (end.tv_sec - start.tv_sec) + (end.tv_nsec - start.tv_nsec) / 1e9;
}

/* --------------------------------------------------------------- *
 *  RGB565 → ARGB8888 row converter
 * --------------------------------------------------------------- */

#if HAVE_NEON
/*
 * Process 8 RGB565 pixels per loop iteration.
 *
 * Per-pixel layout:
 *   src u16:   RRRRR GGGGGG BBBBB
 *   dst u32:   AAAAAAAA RRRRRRRR GGGGGGGG BBBBBBBB  (little-endian: B,G,R,A in memory)
 *
 * Bit-expand 5→8 = (v << 3) | (v >> 2)  exact, but for inspection we use
 * (v << 3) which is what the v0.3.7 scalar code did — keep byte-compat
 * with the old visual output. Same for 6→8 = (v << 2).
 */
static void rgb565_row_to_argb_neon(const uint16_t *src, uint32_t *dst, size_t count) {
    const uint16x8_t mask_g = vdupq_n_u16(0x3F);
    const uint16x8_t mask_b = vdupq_n_u16(0x1F);
    const uint8x8_t  alpha  = vdup_n_u8(0xFF);

    size_t i = 0;
    for (; i + 8 <= count; i += 8) {
        uint16x8_t pixels = vld1q_u16(src + i);
        uint16x8_t r5 = vshrq_n_u16(pixels, 11);
        uint16x8_t g6 = vandq_u16(vshrq_n_u16(pixels, 5), mask_g);
        uint16x8_t b5 = vandq_u16(pixels, mask_b);

        /* Narrow each channel to u8 (high bits were already 0), then
         * shift to fill 8-bit range. */
        uint8x8_t r8 = vshl_n_u8(vmovn_u16(r5), 3);
        uint8x8_t g8 = vshl_n_u8(vmovn_u16(g6), 2);
        uint8x8_t b8 = vshl_n_u8(vmovn_u16(b5), 3);

        /* Interleave as B,G,R,A — little-endian memory order = ARGB on
         * read by the framebuffer. */
        uint8x8x4_t bgra = { { b8, g8, r8, alpha } };
        vst4_u8((uint8_t *)(dst + i), bgra);
    }
    /* Tail (< 8 pixels): scalar */
    for (; i < count; ++i) {
        uint16_t p = src[i];
        uint8_t  r = (uint8_t)(((p >> 11) & 0x1F) << 3);
        uint8_t  g = (uint8_t)(((p >> 5)  & 0x3F) << 2);
        uint8_t  b = (uint8_t)(( p        & 0x1F) << 3);
        dst[i] = (0xFFu << 24) | ((uint32_t)r << 16) | ((uint32_t)g << 8) | b;
    }
}
#endif

static void rgb565_row_to_argb_scalar(const uint16_t *src, uint32_t *dst, size_t count) {
    for (size_t i = 0; i < count; ++i) {
        uint16_t p = src[i];
        uint8_t  r = (uint8_t)(((p >> 11) & 0x1F) << 3);
        uint8_t  g = (uint8_t)(((p >> 5)  & 0x3F) << 2);
        uint8_t  b = (uint8_t)(( p        & 0x1F) << 3);
        dst[i] = (0xFFu << 24) | ((uint32_t)r << 16) | ((uint32_t)g << 8) | b;
    }
}

static inline void rgb565_row_to_argb(const uint16_t *src, uint32_t *dst, size_t count) {
#if HAVE_NEON
    rgb565_row_to_argb_neon(src, dst, count);
#else
    rgb565_row_to_argb_scalar(src, dst, count);
#endif
}

/* --------------------------------------------------------------- *
 *  Render paths
 * --------------------------------------------------------------- */

/* Fast path: src dims == fb dims. Each source row converts directly to
 * the corresponding fb row. NEON-accelerated on aarch64. */
static void render_no_scale(unsigned char *fbp, const unsigned char *image,
                             int width, int height, int line_length) {
    for (int y = 0; y < height; ++y) {
        const uint16_t *src_row = (const uint16_t *)(image + (size_t)y * width * 2);
        uint32_t *dst_row = (uint32_t *)(fbp + (size_t)y * line_length);
        rgb565_row_to_argb(src_row, dst_row, (size_t)width);
    }
}

/* Slow path: src dims differ from fb dims. Scale (nearest-neighbour) to
 * fit while preserving aspect, fill remaining area with black letterbox.
 * Same algorithm as v0.3.7's draw_image; orange filler swapped for black. */
static void render_with_scale(unsigned char *fbp, const unsigned char *image,
                               int src_w, int src_h,
                               int dst_w, int dst_h, int line_length) {
    float aspect = (float)src_w / (float)src_h;
    int scaled_w = dst_w, scaled_h = dst_h;
    if ((float)dst_w / (float)dst_h > aspect) {
        scaled_w = (int)((float)dst_h * aspect);
    } else {
        scaled_h = (int)((float)dst_w / aspect);
    }
    float x_ratio = (float)src_w / (float)scaled_w;
    float y_ratio = (float)src_h / (float)scaled_h;
    int x_off = (dst_w - scaled_w) / 2;
    int y_off = (dst_h - scaled_h) / 2;

    /* Top letterbox */
    for (int y = 0; y < y_off; ++y) {
        uint32_t *dst_row = (uint32_t *)(fbp + (size_t)y * line_length);
        for (int x = 0; x < dst_w; ++x) dst_row[x] = LETTERBOX_ARGB;
    }
    /* Image rows + side letterbox */
    for (int y = 0; y < scaled_h; ++y) {
        int src_y = (int)((float)y * y_ratio);
        uint32_t *dst_row = (uint32_t *)(fbp + (size_t)(y + y_off) * line_length);
        const uint16_t *src_row = (const uint16_t *)(image + (size_t)src_y * src_w * 2);
        for (int x = 0; x < x_off; ++x) dst_row[x] = LETTERBOX_ARGB;
        for (int x = 0; x < scaled_w; ++x) {
            int src_x = (int)((float)x * x_ratio);
            uint16_t p = src_row[src_x];
            uint8_t r = (uint8_t)(((p >> 11) & 0x1F) << 3);
            uint8_t g = (uint8_t)(((p >> 5)  & 0x3F) << 2);
            uint8_t b = (uint8_t)(( p        & 0x1F) << 3);
            dst_row[x + x_off] = (0xFFu << 24) | ((uint32_t)r << 16) | ((uint32_t)g << 8) | b;
        }
        for (int x = x_off + scaled_w; x < dst_w; ++x) dst_row[x] = LETTERBOX_ARGB;
    }
    /* Bottom letterbox */
    for (int y = y_off + scaled_h; y < dst_h; ++y) {
        uint32_t *dst_row = (uint32_t *)(fbp + (size_t)y * line_length);
        for (int x = 0; x < dst_w; ++x) dst_row[x] = LETTERBOX_ARGB;
    }
}

/* mmap source file, parse header, dispatch to fast or slow path,
 * unmap. Returns 0 on success, -1 on any error (already logged). */
static int render_frame(const char *filename, unsigned char *fbp,
                        int fb_w, int fb_h, int line_length) {
    int fd = open(filename, O_RDONLY);
    if (fd == -1) {
        perror("open source");
        return -1;
    }
    struct stat st;
    if (fstat(fd, &st) == -1) {
        perror("fstat source");
        close(fd);
        return -1;
    }
    if ((size_t)st.st_size < HEADER_BYTES) {
        fprintf(stderr, "source too small: %ld bytes (need >= %zu for header)\n",
                (long)st.st_size, (size_t)HEADER_BYTES);
        close(fd);
        return -1;
    }
    void *map = mmap(NULL, (size_t)st.st_size, PROT_READ, MAP_SHARED, fd, 0);
    close(fd);  /* fd not needed after mmap */
    if (map == MAP_FAILED) {
        perror("mmap source");
        return -1;
    }

    int32_t src_w = ((const int32_t *)map)[0];
    int32_t src_h = ((const int32_t *)map)[1];
    size_t pixel_bytes = (size_t)st.st_size - HEADER_BYTES;
    if (src_w <= 0 || src_h <= 0
        || (size_t)src_w * (size_t)src_h * 2 != pixel_bytes) {
        fprintf(stderr, "size mismatch: header w=%d h=%d → %zu bytes; payload=%zu\n",
                src_w, src_h, (size_t)src_w * (size_t)src_h * 2, pixel_bytes);
        munmap(map, (size_t)st.st_size);
        return -1;
    }

    const unsigned char *image = (const unsigned char *)map + HEADER_BYTES;
    if (src_w == fb_w && src_h == fb_h) {
        render_no_scale(fbp, image, src_w, src_h, line_length);
    } else {
        render_with_scale(fbp, image, src_w, src_h, fb_w, fb_h, line_length);
    }

    munmap(map, (size_t)st.st_size);
    return 0;
}

/* --------------------------------------------------------------- *
 *  main
 * --------------------------------------------------------------- */

int main(int argc, char **argv) {
    const char *image_file = (argc > 1) ? argv[1] : DEFAULT_PATH;
    printf("Watching: %s\n", image_file);
    printf("Build: NEON %s\n", HAVE_NEON ? "enabled (aarch64)" : "disabled (scalar fallback)");

    int inotifyFd = inotify_init();
    if (inotifyFd == -1) { perror("inotify_init"); return EXIT_FAILURE; }

    int wd = inotify_add_watch(inotifyFd, image_file, IN_CLOSE_WRITE);
    if (wd == -1) { perror("inotify_add_watch"); return EXIT_FAILURE; }

    int fbfd = open("/dev/fb0", O_RDWR);
    if (fbfd == -1) { perror("open /dev/fb0"); return 1; }

    struct fb_fix_screeninfo finfo;
    if (ioctl(fbfd, FBIOGET_FSCREENINFO, &finfo)) { perror("FBIOGET_FSCREENINFO"); close(fbfd); return 2; }

    struct fb_var_screeninfo vinfo;
    if (ioctl(fbfd, FBIOGET_VSCREENINFO, &vinfo)) { perror("FBIOGET_VSCREENINFO"); close(fbfd); return 3; }

    printf("Framebuffer resolution: %dx%d, Bits per pixel: %d, Line length: %d bytes\n",
           vinfo.xres, vinfo.yres, vinfo.bits_per_pixel, finfo.line_length);

    long int screensize = vinfo.yres_virtual * finfo.line_length;
    char *fbp = (char *)mmap(0, (size_t)screensize, PROT_READ | PROT_WRITE, MAP_SHARED, fbfd, 0);
    if (fbp == MAP_FAILED) { perror("mmap fb"); close(fbfd); return 4; }
    printf("The framebuffer device was mapped to memory successfully.\n");
    printf("Entering main loop, waiting for file events...\n");
    fflush(stdout);

    /* Heartbeat: one summary line per minute so operators can confirm
     * the renderer is alive without per-frame log spam. */
    int frame_count = 0;
    struct timespec last_heartbeat;
    clock_gettime(CLOCK_MONOTONIC, &last_heartbeat);

    char buffer[BUF_LEN];
    while (1) {
        int length = read(inotifyFd, buffer, BUF_LEN);
        if (length < 0) { perror("read inotify"); break; }

        for (int i = 0; i < length; i += EVENT_SIZE + ((struct inotify_event *)&buffer[i])->len) {
            struct inotify_event *event = (struct inotify_event *)&buffer[i];
            if (!(event->mask & IN_CLOSE_WRITE)) continue;

            /* Single-thread render: mmap is fast (page cache), and the
             * inotify read above is blocking — there's nothing else to
             * do, so spawning a per-frame pthread just adds overhead. */
            render_frame(image_file, (unsigned char *)fbp,
                         vinfo.xres, vinfo.yres, finfo.line_length);
            frame_count++;

            struct timespec now;
            clock_gettime(CLOCK_MONOTONIC, &now);
            double since_hb = get_elapsed_time(last_heartbeat, now);
            if (since_hb >= 60.0) {
                printf("[heartbeat] rendered %d frames in last %.0fs\n", frame_count, since_hb);
                fflush(stdout);
                frame_count = 0;
                last_heartbeat = now;
            }
        }
    }

    munmap(fbp, (size_t)screensize);
    close(fbfd);
    close(inotifyFd);
    return 0;
}
