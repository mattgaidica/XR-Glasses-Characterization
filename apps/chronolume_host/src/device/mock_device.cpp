#include "device/device.hpp"

#include <algorithm>

namespace {

class MockDevice final : public Device {
public:
    bool open() override {
        info_.backend = "mock";
        info_.market_name = "Mock Luma Ultra";
        info_.product_id = 0;
        info_.device_type = 2;  // XR_DEVICE_TYPE_VITURE_CARINA
        info_.firmware = "mock";
        info_.display_mode = 0x32;  // 3840x1080@60 SBS
        info_.brightness = 5;
        info_.duty_cycle = 98;
        info_.film = 0.0f;
        info_.connected = true;
        info_.last_error.clear();
        return true;
    }

    void close() override { info_.connected = false; }

    DeviceInfo info() const override { return info_; }

    bool set_brightness(int level) override {
        info_.brightness = std::clamp(level, 0, 8);
        return true;
    }

    bool set_duty_cycle(int percent) override {
        info_.duty_cycle = std::clamp(percent, 0, 100);
        return true;
    }

    bool set_film(float voltage) override {
        info_.film = voltage > 0.0f ? 1.0f : 0.0f;
        return true;
    }

private:
    DeviceInfo info_;
};

}  // namespace

std::unique_ptr<Device> make_mock_device() {
    return std::make_unique<MockDevice>();
}
