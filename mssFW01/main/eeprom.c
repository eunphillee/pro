#include "eeprom.h"

#include "board_pins.h"
#include "driver/i2c_master.h"
#include "esp_log.h"

static const char *TAG = "eeprom";

#define EEPROM_I2C_FREQ_HZ      100000
#define EEPROM_AT24C02C_ADDR    0x50

esp_err_t eeprom_scan(void)
{
    i2c_master_bus_config_t bus_cfg = {
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .i2c_port = I2C_NUM_0,
        .scl_io_num = PIN_I2C_SCL,
        .sda_io_num = PIN_I2C_SDA,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };

    i2c_master_bus_handle_t bus_handle = NULL;
    esp_err_t err = i2c_new_master_bus(&bus_cfg, &bus_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2c_new_master_bus failed: %s", esp_err_to_name(err));
        return err;
    }

    ESP_LOGI(TAG, "I2C bus ready: SDA=GPIO%d, SCL=GPIO%d, %d Hz",
             PIN_I2C_SDA, PIN_I2C_SCL, EEPROM_I2C_FREQ_HZ);

    err = i2c_master_probe(bus_handle, EEPROM_AT24C02C_ADDR, 100);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "AT24C02C detected at address 0x%02X", EEPROM_AT24C02C_ADDR);
    } else if (err == ESP_ERR_NOT_FOUND) {
        ESP_LOGW(TAG, "AT24C02C not found at address 0x%02X", EEPROM_AT24C02C_ADDR);
    } else {
        ESP_LOGE(TAG, "I2C probe failed: %s", esp_err_to_name(err));
    }

    i2c_del_master_bus(bus_handle);
    return err;
}
