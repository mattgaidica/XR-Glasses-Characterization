#include "display/macos_colorspace.h"

void chronolume_set_device_rgb_color_space(struct GLFWwindow* window) {
    // Do not set NSWindow.colorSpace on an NSOpenGL window.
    // deviceRGBColorSpace makes AppKit throw CGSSetSurfaceColorSpace / 1000
    // on window move on recent macOS. Gate 0 checks OS color management via
    // framebuffer readback (V) and the spectrometer instead.
    (void)window;
}
