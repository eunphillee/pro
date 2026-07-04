#include "buzzer.h"
#include "eeprom.h"
#include "input.h"
#include "lte_modem.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "app_main";

#define FW_VERSION      "0.1.0"
#define BOARD_NAME      "ESP32-S3-WROOM-1-N16R2"
#define TARGET_NAME     "esp32s3"

static void print_boot_banner(void)
{
    ESP_LOGI(TAG, "MSS03 Alarm Board Boot OK");
    ESP_LOGI(TAG, "FW Version: %s", FW_VERSION);
    ESP_LOGI(TAG, "Target: %s", BOARD_NAME);
    ESP_LOGI(TAG, "Chip target: %s", TARGET_NAME);
}

void app_main(void)
{
    print_boot_banner();

    ESP_ERROR_CHECK(buzzer_init());
    ESP_ERROR_CHECK(input_init());

    ESP_ERROR_CHECK(buzzer_test());

    esp_err_t eeprom_err = eeprom_scan();
    if (eeprom_err != ESP_OK && eeprom_err != ESP_ERR_NOT_FOUND) {
        ESP_LOGW(TAG, "EEPROM scan error: %s", esp_err_to_name(eeprom_err));
    }

    ESP_ERROR_CHECK(lte_modem_init());
    ESP_ERROR_CHECK(lte_modem_powerkey_pulse());

    ESP_LOGI(TAG, "waiting 10 seconds for LTE modem boot...");
    vTaskDelay(pdMS_TO_TICKS(10000));

    esp_err_t at_err = lte_modem_send_at_test();
    if (at_err != ESP_OK) {
        ESP_LOGW(TAG, "AT test failed: %s", esp_err_to_name(at_err));
    } else {
        lte_modem_prepare_for_sms();
        lte_modem_set_ready(true);
    }

    ESP_ERROR_CHECK(input_start_monitor());
    input_notify_modem_ready();

    ESP_LOGI(TAG, "bring-up sequence complete");
}
