#include "buzzer.h"

#include "board_pins.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "buzzer";

esp_err_t buzzer_init(void)
{
    gpio_config_t cfg = {
        .pin_bit_mask = 1ULL << PIN_BUZZER_CTRL,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };

    esp_err_t err = gpio_config(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "gpio_config failed: %s", esp_err_to_name(err));
        return err;
    }

    gpio_set_level(PIN_BUZZER_CTRL, 0);
    ESP_LOGI(TAG, "buzzer GPIO%d initialized", PIN_BUZZER_CTRL);
    return ESP_OK;
}

esp_err_t buzzer_test(void)
{
    ESP_LOGI(TAG, "buzzer test: ON 100ms");
    gpio_set_level(PIN_BUZZER_CTRL, 1);
    vTaskDelay(pdMS_TO_TICKS(100));
    gpio_set_level(PIN_BUZZER_CTRL, 0);
    ESP_LOGI(TAG, "buzzer test: OFF");
    return ESP_OK;
}
