#include "input.h"

#include <string.h>

#include "alarm_config.h"
#include "board_pins.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lte_modem.h"

static const char *TAG = "input";

#define MSG_NORMAL          "정상 상태 또는 입력 없음"

static input_event_t s_input_event;
static uint8_t s_prev_state_mask = 0;
static uint8_t s_last_sent_mask = 0;
static bool s_pending_sms = false;
static uint8_t s_pending_mask = 0;
static char s_pending_message[128];

static const char *state_on_off(bool on)
{
    return on ? "ON" : "OFF";
}

void input_read_state(input_state_t *out)
{
    out->raw_in1 = gpio_get_level(PIN_RELAY_IN1);
    out->raw_in2 = gpio_get_level(PIN_RELAY_IN2);
    out->raw_in3 = gpio_get_level(PIN_RELAY_IN3);

    /* LOW Active: raw 0 = ON, raw 1 = OFF */
    out->in1_on = (out->raw_in1 == 0);
    out->in2_on = (out->raw_in2 == 0);
    out->in3_on = (out->raw_in3 == 0);
}

static int input_mask_to_message_index(uint8_t mask)
{
    switch (mask) {
    case 1: return 0;
    case 2: return 1;
    case 4: return 2;
    case 3: return 3;
    case 6: return 4;
    case 5: return 5;
    case 7: return 6;
    default: return -1;
    }
}

const char *input_get_alarm_message(bool in1_on, bool in2_on, bool in3_on)
{
    uint8_t mask = (uint8_t)((in1_on ? 1U : 0U) |
                             (in2_on ? 2U : 0U) |
                             (in3_on ? 4U : 0U));
    int index = input_mask_to_message_index(mask);

    if (index < 0) {
        return MSG_NORMAL;
    }

    return alarm_config_get_message(index);
}

const input_event_t *input_get_event(void)
{
    return &s_input_event;
}

static uint8_t input_state_to_mask(const input_state_t *state)
{
    return (uint8_t)((state->in1_on ? 1U : 0U) |
                     (state->in2_on ? 2U : 0U) |
                     (state->in3_on ? 4U : 0U));
}

static void input_publish_event(const input_state_t *state)
{
    s_input_event.state = *state;
    s_input_event.message = input_get_alarm_message(state->in1_on, state->in2_on, state->in3_on);
    s_input_event.valid = true;
}

static void input_log_state_change(const input_state_t *state)
{
    ESP_LOGI(TAG,
             "input raw: IN1=%d, IN2=%d, IN3=%d / state: IN1=%s, IN2=%s, IN3=%s / message: %s",
             state->raw_in1, state->raw_in2, state->raw_in3,
             state_on_off(state->in1_on),
             state_on_off(state->in2_on),
             state_on_off(state->in3_on),
             s_input_event.message);
}

static void input_dispatch_sms(uint8_t mask, const char *message)
{
    if (mask == 0 || message == NULL) {
        return;
    }

    ESP_LOGI(TAG, "input event: mask=%u, message=%s", mask, message);

    if (!lte_modem_is_ready()) {
        s_pending_sms = true;
        s_pending_mask = mask;
        strncpy(s_pending_message, message, sizeof(s_pending_message) - 1);
        s_pending_message[sizeof(s_pending_message) - 1] = '\0';
        ESP_LOGI(TAG, "modem not ready, SMS pending (mask=%u)", mask);
        return;
    }

    esp_err_t err = lte_modem_send_alarm_sms(message);
    if (err != ESP_OK && err != ESP_ERR_INVALID_ARG) {
        ESP_LOGE(TAG, "sms send failed: %s", esp_err_to_name(err));
    }
}

void input_notify_modem_ready(void)
{
    if (!s_pending_sms || s_pending_mask == 0) {
        return;
    }

    if (s_pending_mask == s_last_sent_mask) {
        ESP_LOGI(TAG, "flushing pending SMS (mask=%u): %s", s_pending_mask, s_pending_message);
        esp_err_t err = lte_modem_send_alarm_sms(s_pending_message);
        if (err != ESP_OK && err != ESP_ERR_INVALID_ARG) {
            ESP_LOGE(TAG, "pending sms send failed: %s", esp_err_to_name(err));
        }
    }

    s_pending_sms = false;
    s_pending_mask = 0;
}

static void input_monitor_task(void *arg)
{
    (void)arg;

    while (true) {
        input_state_t state;
        input_read_state(&state);

        uint8_t mask = input_state_to_mask(&state);

        if (mask != s_prev_state_mask) {
            input_publish_event(&state);
            input_log_state_change(&state);
            s_prev_state_mask = mask;
        }

        if (mask == 0) {
            s_last_sent_mask = 0;
            s_pending_sms = false;
            s_pending_mask = 0;
        } else if (mask != s_last_sent_mask) {
            const char *message = input_get_alarm_message(state.in1_on, state.in2_on, state.in3_on);
            s_last_sent_mask = mask;
            input_dispatch_sms(mask, message);
        }

        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

esp_err_t input_init(void)
{
    gpio_config_t cfg = {
        .pin_bit_mask = (1ULL << PIN_RELAY_IN1) |
                        (1ULL << PIN_RELAY_IN2) |
                        (1ULL << PIN_RELAY_IN3),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };

    esp_err_t err = gpio_config(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "gpio_config failed: %s", esp_err_to_name(err));
        return err;
    }

    ESP_LOGI(TAG, "input GPIO%d,%d,%d initialized (LOW Active)", PIN_RELAY_IN1, PIN_RELAY_IN2, PIN_RELAY_IN3);
    return ESP_OK;
}

esp_err_t input_start_monitor(void)
{
    BaseType_t ok = xTaskCreate(
        input_monitor_task,
        "input_mon",
        4096,
        NULL,
        5,
        NULL);

    if (ok != pdPASS) {
        ESP_LOGE(TAG, "failed to create input monitor task");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "input monitor task started (1s interval, SMS once per mask)");
    return ESP_OK;
}
