/*
 * image_updater — watches an RGB565 file and pushes it to /dev/fb0.
 *
 * Companion to the Python vision binary: the Python side writes the
 * composed operator-screen image as RGB565 (with a 2x int32 width/height
 * header), and this process renders it on the framebuffer. inotify
 * IN_CLOSE_WRITE drives the refresh.
 *
 * Default watch path: /home/pi/output_image.rgb565
 * Override via argv[1] if you've moved it.
 */

#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/ioctl.h>
#include <linux/fb.h>
#include <unistd.h>
#include <string.h>
#include <sys/inotify.h>
#include <pthread.h>
#include <time.h>

#define EVENT_SIZE   (sizeof(struct inotify_event))
#define BUF_LEN      (1024 * (EVENT_SIZE + 16))
#define DEFAULT_PATH "/home/pi/output_image.rgb565"

typedef struct {
    const char *filename;
    unsigned char *fbp;
    struct fb_var_screeninfo vinfo;
    struct fb_fix_screeninfo finfo;
    long int screensize;
} ThreadArgs;

double get_elapsed_time(struct timespec start, struct timespec end) {
    return (end.tv_sec - start.tv_sec) + (end.tv_nsec - start.tv_nsec) / 1e9;
}

int load_rgb565(const char *filename, unsigned char **image, size_t *size, int *width, int *height) {
    struct timespec start, end;
    clock_gettime(CLOCK_MONOTONIC, &start);

    int fd = open(filename, O_RDONLY);
    if (fd == -1) {
        perror("Error opening file");
        return -1;
    }

    clock_gettime(CLOCK_MONOTONIC, &end);
    printf("Opening file took %.3f seconds\n", get_elapsed_time(start, end));

    clock_gettime(CLOCK_MONOTONIC, &start);

    struct stat st;
    if (fstat(fd, &st) == -1) {
        perror("Error getting file size");
        close(fd);
        return -1;
    }

    clock_gettime(CLOCK_MONOTONIC, &end);
    printf("Getting file size took %.3f seconds\n", get_elapsed_time(start, end));
    printf("File size: %ld bytes\n", st.st_size);

    clock_gettime(CLOCK_MONOTONIC, &start);

    if (read(fd, width, sizeof(int)) != sizeof(int) || read(fd, height, sizeof(int)) != sizeof(int)) {
        perror("Error reading width/height");
        close(fd);
        return -1;
    }
    printf("Image width: %d, height: %d\n", *width, *height);

    *size = st.st_size - 2 * sizeof(int);

    if (*size != (*width) * (*height) * 2) {
        fprintf(stderr, "File size mismatch: expected %d, got %zu\n", (*width) * (*height) * 2, *size);
        close(fd);
        return -1;
    }

    *image = (unsigned char *)malloc(*size);
    if (*image == NULL) {
        perror("Error allocating memory");
        close(fd);
        return -1;
    }

    if (lseek(fd, 2 * sizeof(int), SEEK_SET) == -1) {
        perror("Error seeking in file");
        close(fd);
        free(*image);
        return -1;
    }

    if (read(fd, *image, *size) != *size) {
        perror("Error reading image data");
        close(fd);
        free(*image);
        return -1;
    }

    clock_gettime(CLOCK_MONOTONIC, &end);
    printf("Reading file took %.3f seconds\n", get_elapsed_time(start, end));

    close(fd);
    return 0;
}
void clear_screen(unsigned char *fbp, int screen_width, int screen_height, int line_length, unsigned int color) {
    for (int y = 0; y < screen_height; y++) {
        unsigned int *dst_row = (unsigned int *)(fbp + y * line_length);
        for (int x = 0; x < screen_width; x++) {
            dst_row[x] = color;
        }
    }
}

 
void draw_image(unsigned char *fbp, unsigned char *image, int src_width, int src_height, int dst_width, int dst_height, int line_length) {
    struct timespec start, end;
    clock_gettime(CLOCK_MONOTONIC, &start);

    // 计算原始图像的宽高比
    float aspect_ratio = (float)src_width / src_height;

    // 计算目标宽度和高度以保持宽高比
    int scaled_width = dst_width;
    int scaled_height = dst_height;

    if ((float)dst_width / dst_height > aspect_ratio) {
        // 以高度为基准缩放
        scaled_width = (int)(dst_height * aspect_ratio);
    } else {
        // 以宽度为基准缩放
        scaled_height = (int)(dst_width / aspect_ratio);
    }

    // 计算 x_ratio 和 y_ratio 用于图像缩放
    float x_ratio = (float)src_width / scaled_width;
    float y_ratio = (float)src_height / scaled_height;

    // 计算目标图像在屏幕中的位置，以居中显示
    int x_offset = (dst_width - scaled_width) / 2;
    int y_offset = (dst_height - scaled_height) / 2;

    int x, y;

    // 填充顶部橘黄色区域
    for (y = 0; y < y_offset; y++) {
        unsigned int *dst_row = (unsigned int *)(fbp + y * line_length);
        for (x = 0; x < dst_width; x++) {
            dst_row[x] = 0xFFA500FF; // ARGB 格式
        }
    }

    // 绘制图像和填充左右两侧橘黄色区域
    for (y = 0; y < scaled_height; y++) {
        int src_y = (int)(y * y_ratio);
        unsigned int *dst_row = (unsigned int *)(fbp + (y + y_offset) * line_length);
        unsigned short *src_row = (unsigned short *)(image + src_y * src_width * 2);

        // 填充左侧橘黄色区域
        for (x = 0; x < x_offset; x++) {
            dst_row[x] = 0xFFA500FF; // ARGB 格式
        }

        // 绘制图像
        for (x = 0; x < scaled_width; x++) {
            int src_x = (int)(x * x_ratio);
            unsigned short pixel565 = src_row[src_x];
            unsigned char r = (pixel565 >> 11) & 0x1F;
            unsigned char g = (pixel565 >> 5) & 0x3F;
            unsigned char b = pixel565 & 0x1F;

            // Convert RGB565 to 32-bit (ARGB)
            dst_row[x + x_offset] = (0xFF << 24) | ((r << 3) << 16) | ((g << 2) << 8) | (b << 3);
        }

        // 填充右侧橘黄色区域
        for (x = x_offset + scaled_width; x < dst_width; x++) {
            dst_row[x] = 0xFFA500FF; // ARGB 格式
        }
    }

    // 填充底部橘黄色区域
    for (y = y_offset + scaled_height; y < dst_height; y++) {
        unsigned int *dst_row = (unsigned int *)(fbp + y * line_length);
        for (x = 0; x < dst_width; x++) {
            dst_row[x] = 0xFFA500FF; // ARGB 格式
        }
    }

    clock_gettime(CLOCK_MONOTONIC, &end);
    printf("Drawing image took %.3f seconds\n", get_elapsed_time(start, end));
}

void* update_image(void *args) {
    struct timespec start, end;
    clock_gettime(CLOCK_MONOTONIC, &start);

    ThreadArgs *threadArgs = (ThreadArgs *) args;
    unsigned char *image;
    size_t size;
    int width, height;

    if (load_rgb565(threadArgs->filename, &image, &size, &width, &height) == 0) {
        clock_gettime(CLOCK_MONOTONIC, &end);
        printf("Loading image took %.3f seconds\n", get_elapsed_time(start, end));
        printf("Loaded image size: %zu bytes, Expected image size: %ld bytes\n", size, threadArgs->screensize);

        clock_gettime(CLOCK_MONOTONIC, &start);
        draw_image(threadArgs->fbp, image, width, height, threadArgs->vinfo.xres, threadArgs->vinfo.yres, threadArgs->finfo.line_length);
        clock_gettime(CLOCK_MONOTONIC, &end);
        printf("Total drawing time: %.3f seconds\n", get_elapsed_time(start, end));

        free(image);
    }
    return NULL;
}
 
int main(int argc, char **argv) {
    const char *image_file = (argc > 1) ? argv[1] : DEFAULT_PATH;
    printf("Watching: %s\n", image_file);

    int fbfd = 0, inotifyFd, wd;
    struct fb_var_screeninfo vinfo;
    struct fb_fix_screeninfo finfo;
    long int screensize = 0;
    char *fbp = 0;
    char buffer[BUF_LEN];

    struct timespec start, end, last_trigger, current_trigger;
    clock_gettime(CLOCK_MONOTONIC, &start);
    
    inotifyFd = inotify_init();
    if (inotifyFd == -1) {
        perror("inotify_init");
        exit(EXIT_FAILURE);
    }

    wd = inotify_add_watch(inotifyFd, image_file, IN_CLOSE_WRITE);
    if (wd == -1) {
        perror("inotify_add_watch");
        exit(EXIT_FAILURE);
    }

    fbfd = open("/dev/fb0", O_RDWR);
    if (fbfd == -1) {
        perror("Error: cannot open framebuffer device");
        return 1;
    }

    if (ioctl(fbfd, FBIOGET_FSCREENINFO, &finfo)) {
        perror("Error reading fixed information");
        close(fbfd);
        return 2;
    }

    if (ioctl(fbfd, FBIOGET_VSCREENINFO, &vinfo)) {
        perror("Error reading variable information");
        close(fbfd);
        return 3;
    }

    printf("Framebuffer resolution: %dx%d, Bits per pixel: %d, Line length: %d bytes\n",
           vinfo.xres, vinfo.yres, vinfo.bits_per_pixel, finfo.line_length);

    screensize = vinfo.yres_virtual * finfo.line_length;

    fbp = (char *)mmap(0, screensize, PROT_READ | PROT_WRITE, MAP_SHARED, fbfd, 0);
    if (fbp == MAP_FAILED) {
        perror("Error: failed to map framebuffer device to memory");
        close(fbfd);
        return 4;
    }
    printf("The framebuffer device was mapped to memory successfully.\n");

    clock_gettime(CLOCK_MONOTONIC, &end);
    printf("Initialization took %.3f seconds\n", get_elapsed_time(start, end));

    ThreadArgs args = {image_file, fbp, vinfo, finfo, screensize};
    pthread_t thread_id;

    clock_gettime(CLOCK_MONOTONIC, &last_trigger);

    printf("Entering main loop, waiting for file events...\n");

    while (1) {
        printf("Waiting for inotify events...\n");
        int length = read(inotifyFd, buffer, BUF_LEN);
        if (length < 0) {
            perror("read");
            break;
        }

        printf("Read %d bytes from inotify file descriptor\n", length);

        for (int i = 0; i < length; i += EVENT_SIZE + ((struct inotify_event*)&buffer[i])->len) {
            struct inotify_event *event = (struct inotify_event*)&buffer[i];
            if (event->mask & IN_CLOSE_WRITE) {
                clock_gettime(CLOCK_MONOTONIC, &current_trigger);
                printf("Detected file modification, updating image...\n");
                printf("Time since last trigger: %.3f seconds\n", get_elapsed_time(last_trigger, current_trigger));
                last_trigger = current_trigger;

                clock_gettime(CLOCK_MONOTONIC, &start);
                pthread_create(&thread_id, NULL, update_image, &args);
                clock_gettime(CLOCK_MONOTONIC, &end);
                printf("Trigger time: %.3f seconds\n", get_elapsed_time(start, end));
                pthread_detach(thread_id);
            }
        }
    }

    munmap(fbp, screensize);
    close(fbfd);
    close(inotifyFd);

    return 0;
}
