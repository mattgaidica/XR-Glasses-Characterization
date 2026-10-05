#pragma once

#include <memory>
#include <string>

struct DeviceInfo {
    std::string backend;
    std::string market_name;
    int product_id = 0;
    int device_type = -1;
    std::string firmware;
    int display_mode = 0;
    int brightness = 0;
    int duty_cycle = 100;
    float film = 0.0f;
    int wear_status = -1;  // 1 worn, 0 not worn, -1 not reported by the device
    bool connected = false;
    std::string last_error;
};

class Device {
public:
    virtual ~Device() = default;
    virtual bool open() = 0;
    virtual void close() = 0;
    virtual DeviceInfo info() const = 0;
    virtual bool set_brightness(int level) = 0;
    virtual bool set_duty_cycle(int percent) = 0;
    virtual bool set_film(float voltage) = 0;
};

std::unique_ptr<Device> make_mock_device();

#ifdef CHRONOLUME_WITH_VITURE
std::unique_ptr<Device> make_viture_device();
#endif
