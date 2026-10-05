#include "device/device.hpp"

#include "viture_device_carina.h"
#include "viture_glasses_provider.h"
#include "viture_protocol_public.h"
#include "viture_result.h"

#if defined(_WIN32)
#include <windows.h>
#include <setupapi.h>
#else
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/IOKitLib.h>
#include <IOKit/usb/IOUSBLib.h>

#include <dlfcn.h>
#endif

#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace {

constexpr int kVitureVid = 0x35CA;

// GlassStateCallback carries no user pointer and runs on an SDK thread.
std::atomic<int> g_wear_status{-1};

void on_glass_state(int id, int value) {
    if (id == VITURE_CALLBACK_ID_WEAR_STATUS) {
        g_wear_status.store(value);
    }
}

#if defined(_WIN32)
constexpr const char* kGlassesLib = "glasses.dll";
constexpr const char* kCarinaLib = "carina_vio.dll";

void* open_lib(const std::string& path, bool /*global*/) {
    // Altered search path resolves the SDK's dependent DLLs from its own folder, but only
    // for backslash-separated absolute paths.
    std::string native = path;
    for (char& c : native) {
        if (c == '/') {
            c = '\\';
        }
    }
    return reinterpret_cast<void*>(
        LoadLibraryExA(native.c_str(), nullptr, LOAD_WITH_ALTERED_SEARCH_PATH));
}

void* find_sym(void* lib, const char* name) {
    return reinterpret_cast<void*>(GetProcAddress(static_cast<HMODULE>(lib), name));
}

std::string lib_error() {
    return "Win32 error " + std::to_string(GetLastError());
}
#else
constexpr const char* kGlassesLib = "libglasses.dylib";
constexpr const char* kCarinaLib = "libcarina_vio.dylib";

void* open_lib(const std::string& path, bool global) {
    return dlopen(path.c_str(), RTLD_NOW | (global ? RTLD_GLOBAL : 0));
}

void* find_sym(void* lib, const char* name) {
    dlerror();
    return dlsym(lib, name);
}

std::string lib_error() {
    const char* e = dlerror();
    return e ? e : "null";
}
#endif

struct VitureApi {
    void* glasses = nullptr;
    void* carina = nullptr;

    XRDeviceProviderHandle (*create)(int) = nullptr;
    int (*initialize)(XRDeviceProviderHandle, const char*, const char*) = nullptr;
    int (*start)(XRDeviceProviderHandle) = nullptr;
    int (*stop)(XRDeviceProviderHandle) = nullptr;
    int (*shutdown)(XRDeviceProviderHandle) = nullptr;
    void (*destroy)(XRDeviceProviderHandle) = nullptr;
    int (*get_device_type)(XRDeviceProviderHandle) = nullptr;
    int (*is_product_id_valid)(int) = nullptr;
    int (*get_market_name)(int, char*, int*) = nullptr;
    void (*set_log_level)(int) = nullptr;
    int (*set_dof_type_carina)(XRDeviceProviderHandle, int) = nullptr;
    int (*get_display_mode)(XRDeviceProviderHandle) = nullptr;
    int (*get_brightness_level)(XRDeviceProviderHandle) = nullptr;
    int (*set_brightness_level)(XRDeviceProviderHandle, int) = nullptr;
    int (*get_duty_cycle)(XRDeviceProviderHandle) = nullptr;
    int (*set_duty_cycle)(XRDeviceProviderHandle, int) = nullptr;
    int (*get_film_mode)(XRDeviceProviderHandle, float*) = nullptr;
    int (*set_film_mode)(XRDeviceProviderHandle, float) = nullptr;
    int (*get_glasses_version)(XRDeviceProviderHandle, char*, int*) = nullptr;
    const char* (*get_version_string)() = nullptr;
    // Optional: wear status is Gen2-only.
    int (*register_state_callback)(XRDeviceProviderHandle, GlassStateCallback) = nullptr;
    int (*get_wear_status)(XRDeviceProviderHandle, uint8_t*) = nullptr;
};

template <typename T>
bool load_sym(void* lib, T* out, const char* name, std::string* err) {
    *out = reinterpret_cast<T>(find_sym(lib, name));
    if (!*out) {
        *err = std::string("symbol ") + name + ": " + lib_error();
        return false;
    }
    return true;
}

bool load_api(VitureApi* api, std::string* err) {
    const std::string dir = CHRONOLUME_VITURE_LIBDIR;
    api->carina = open_lib(dir + "/" + kCarinaLib, true);
    api->glasses = open_lib(dir + "/" + kGlassesLib, false);
    if (!api->glasses) {
        *err = std::string("load ") + dir + "/" + kGlassesLib + ": " + lib_error();
        return false;
    }
#define LOAD(field, symbol) \
    if (!load_sym(api->glasses, &api->field, symbol, err)) return false
    LOAD(create, "xr_device_provider_create");
    LOAD(initialize, "xr_device_provider_initialize");
    LOAD(start, "xr_device_provider_start");
    LOAD(stop, "xr_device_provider_stop");
    LOAD(shutdown, "xr_device_provider_shutdown");
    LOAD(destroy, "xr_device_provider_destroy");
    LOAD(get_device_type, "xr_device_provider_get_device_type");
    LOAD(is_product_id_valid, "xr_device_provider_is_product_id_valid");
    LOAD(get_market_name, "xr_device_provider_get_market_name");
    LOAD(set_log_level, "xr_device_provider_set_log_level");
    LOAD(set_dof_type_carina, "xr_device_provider_set_dof_type_carina");
    LOAD(get_display_mode, "xr_device_provider_get_display_mode");
    LOAD(get_brightness_level, "xr_device_provider_get_brightness_level");
    LOAD(set_brightness_level, "xr_device_provider_set_brightness_level");
    LOAD(get_duty_cycle, "xr_device_provider_get_duty_cycle");
    LOAD(set_duty_cycle, "xr_device_provider_set_duty_cycle");
    LOAD(get_film_mode, "xr_device_provider_get_film_mode");
    LOAD(set_film_mode, "xr_device_provider_set_film_mode");
    LOAD(get_glasses_version, "xr_device_provider_get_glasses_version");
    LOAD(get_version_string, "GetVersionString");
#undef LOAD
    api->register_state_callback = reinterpret_cast<decltype(api->register_state_callback)>(
        find_sym(api->glasses, "xr_device_provider_register_state_callback"));
    api->get_wear_status = reinterpret_cast<decltype(api->get_wear_status)>(
        find_sym(api->glasses, "xr_device_provider_get_wear_status"));
    return true;
}

void add_pid(const VitureApi& api, int pid, std::vector<int>* pids) {
    if (!api.is_product_id_valid(pid)) {
        return;
    }
    for (int existing : *pids) {
        if (existing == pid) {
            return;
        }
    }
    pids->push_back(pid);
}

#if defined(_WIN32)
std::vector<int> enumerate_viture_pids(const VitureApi& api) {
    std::vector<int> pids;
    HDEVINFO devs = SetupDiGetClassDevsA(nullptr, "USB", nullptr, DIGCF_PRESENT | DIGCF_ALLCLASSES);
    if (devs == INVALID_HANDLE_VALUE) {
        return pids;
    }
    SP_DEVINFO_DATA dev{};
    dev.cbSize = sizeof(dev);
    for (DWORD i = 0; SetupDiEnumDeviceInfo(devs, i, &dev); ++i) {
        BYTE buf[4096] = {};
        DWORD reg_type = 0;
        if (!SetupDiGetDeviceRegistryPropertyA(devs, &dev, SPDRP_HARDWAREID, &reg_type, buf,
                                               sizeof(buf) - 2, nullptr)) {
            continue;
        }
        for (const char* p = reinterpret_cast<const char*>(buf); *p; p += std::strlen(p) + 1) {
            const char* vid_pos = std::strstr(p, "VID_");
            const char* pid_pos = std::strstr(p, "PID_");
            unsigned int vid = 0;
            unsigned int pid = 0;
            if (vid_pos && pid_pos && std::sscanf(vid_pos + 4, "%4x", &vid) == 1 &&
                std::sscanf(pid_pos + 4, "%4x", &pid) == 1 &&
                vid == static_cast<unsigned int>(kVitureVid)) {
                add_pid(api, static_cast<int>(pid), &pids);
            }
        }
    }
    SetupDiDestroyDeviceInfoList(devs);
    return pids;
}
#else
std::vector<int> enumerate_viture_pids(const VitureApi& api) {
    std::vector<int> pids;
    CFMutableDictionaryRef matching = IOServiceMatching(kIOUSBDeviceClassName);
    if (!matching) {
        return pids;
    }

    io_iterator_t iter = 0;
    if (IOServiceGetMatchingServices(kIOMainPortDefault, matching, &iter) != KERN_SUCCESS) {
        return pids;
    }

    io_service_t service;
    while ((service = IOIteratorNext(iter))) {
        auto number_for = [&](CFStringRef key) -> int {
            CFNumberRef num = static_cast<CFNumberRef>(
                IORegistryEntryCreateCFProperty(service, key, kCFAllocatorDefault, 0));
            if (!num) {
                return 0;
            }
            int value = 0;
            CFNumberGetValue(num, kCFNumberIntType, &value);
            CFRelease(num);
            return value;
        };
        const int vid = number_for(CFSTR(kUSBVendorID));
        const int pid = number_for(CFSTR(kUSBProductID));
        IOObjectRelease(service);
        if (vid != kVitureVid) {
            continue;
        }
        add_pid(api, pid, &pids);
    }
    IOObjectRelease(iter);
    return pids;
}
#endif

class VitureDevice final : public Device {
public:
    ~VitureDevice() override { close(); }

    bool open() override {
        close();
        if (!load_api(&api_, &info_.last_error)) {
            return false;
        }
        api_.set_log_level(LOG_LEVEL_ERROR);

        const std::vector<int> pids = enumerate_viture_pids(api_);
        if (pids.empty()) {
            info_.last_error = "no VITURE USB device (VID 0x35CA) with a valid PID";
            return false;
        }

        info_.product_id = pids.front();
        handle_ = api_.create(info_.product_id);
        if (!handle_) {
            info_.last_error = "xr_device_provider_create failed";
            return false;
        }

        info_.device_type = api_.get_device_type(handle_);
        if (info_.device_type == XR_DEVICE_TYPE_VITURE_CARINA) {
            api_.set_dof_type_carina(handle_, 0);
        }

        int rc = api_.initialize(handle_, nullptr, nullptr);
        if (rc != VITURE_GLASSES_SUCCESS) {
            info_.last_error = "initialize failed: " + std::to_string(rc);
            api_.destroy(handle_);
            handle_ = nullptr;
            return false;
        }
        rc = api_.start(handle_);
        if (rc != VITURE_GLASSES_SUCCESS) {
            info_.last_error = "start failed: " + std::to_string(rc);
            api_.shutdown(handle_);
            api_.destroy(handle_);
            handle_ = nullptr;
            return false;
        }
        started_ = true;

        char name[64] = {};
        int name_len = static_cast<int>(sizeof(name));
        if (api_.get_market_name(info_.product_id, name, &name_len) == VITURE_GLASSES_SUCCESS) {
            info_.market_name = name;
        } else {
            info_.market_name = "VITURE";
        }

        char fw[128] = {};
        int fw_len = static_cast<int>(sizeof(fw));
        if (api_.get_glasses_version(handle_, fw, &fw_len) == VITURE_GLASSES_SUCCESS) {
            info_.firmware = fw;
        }

        const int mode = api_.get_display_mode(handle_);
        if (mode >= 0) {
            info_.display_mode = mode;
        }
        const int bri = api_.get_brightness_level(handle_);
        if (bri >= 0) {
            info_.brightness = bri;
        }
        const int duty = api_.get_duty_cycle(handle_);
        if (duty >= 0) {
            info_.duty_cycle = duty;
        }
        float film = 0.0f;
        if (api_.get_film_mode(handle_, &film) == VITURE_GLASSES_SUCCESS) {
            info_.film = film;
        }
        g_wear_status.store(-1);
        if (api_.register_state_callback) {
            api_.register_state_callback(handle_, on_glass_state);
        }
        uint8_t wear = 0;
        if (api_.get_wear_status) {
            const int wear_rc = api_.get_wear_status(handle_, &wear);
            if (wear_rc == VITURE_GLASSES_SUCCESS) {
                g_wear_status.store(wear);
            } else {
                std::fprintf(stderr, "wear status not available: %d\n", wear_rc);
            }
        }

        info_.backend = std::string("viture ") + api_.get_version_string();
        info_.connected = true;
        info_.last_error.clear();
        return true;
    }

    void close() override {
        if (handle_) {
            if (started_) {
                api_.stop(handle_);
                api_.shutdown(handle_);
            }
            api_.destroy(handle_);
            handle_ = nullptr;
        }
        started_ = false;
        info_.connected = false;
    }

    DeviceInfo info() const override {
        DeviceInfo out = info_;
        out.wear_status = g_wear_status.load();
        return out;
    }

    bool set_brightness(int level) override {
        if (!handle_ || !started_) {
            return false;
        }
        const int rc = api_.set_brightness_level(handle_, level);
        if (rc != VITURE_GLASSES_SUCCESS) {
            info_.last_error = "set_brightness failed: " + std::to_string(rc);
            return false;
        }
        info_.brightness = level;
        // Re-read so the log shows whether the brightness change also moved the duty cycle.
        const int duty = api_.get_duty_cycle(handle_);
        if (duty >= 0) {
            info_.duty_cycle = duty;
        }
        return true;
    }

    bool set_duty_cycle(int percent) override {
        if (!handle_ || !started_) {
            return false;
        }
        const int rc = api_.set_duty_cycle(handle_, percent);
        if (rc != VITURE_GLASSES_SUCCESS) {
            info_.last_error = "set_duty_cycle failed: " + std::to_string(rc);
            return false;
        }
        // Log what the glasses report, not what was requested.
        const int actual = api_.get_duty_cycle(handle_);
        info_.duty_cycle = actual >= 0 ? actual : percent;
        return true;
    }

    bool set_film(float voltage) override {
        if (!handle_ || !started_) {
            return false;
        }
        const int rc = api_.set_film_mode(handle_, voltage);
        if (rc != VITURE_GLASSES_SUCCESS) {
            info_.last_error = "set_film failed: " + std::to_string(rc);
            return false;
        }
        info_.film = voltage > 0.0f ? 1.0f : 0.0f;
        return true;
    }

private:
    VitureApi api_;
    XRDeviceProviderHandle handle_ = nullptr;
    bool started_ = false;
    DeviceInfo info_;
};

}  // namespace

std::unique_ptr<Device> make_viture_device() {
    return std::make_unique<VitureDevice>();
}
