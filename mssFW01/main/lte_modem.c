#include "lte_modem.h"

#include <stdio.h>
#include <string.h>

#include "board_pins.h"
#include "driver/gpio.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

static const char *TAG = "lte_modem";

#define LTE_UART_PORT               UART_NUM_2
#define LTE_UART_BAUD               115200
#define LTE_UART_RX_BUF_SIZE        1024
#define LTE_AT_RESPONSE_TIMEOUT_MS  3000
#define LTE_AT_RETRY_MAX            10
#define LTE_AT_RETRY_INTERVAL_MS    1000
#define LTE_CTRL_PULSE_MS           2200
#define LTE_SMS_CMGS_TIMEOUT_MS     60000
#define LTE_SMS_PROMPT_TIMEOUT_MS   30000
#define LTE_SMS_QUEUE_LEN           2
#define LTE_SMS_MSG_MAX_LEN         128
#define LTE_SMS_PHONE_MAX_LEN       20
#define LTE_UCS2_HEX_MAX_LEN        512

typedef struct {
    char phone[LTE_SMS_PHONE_MAX_LEN];
    char message[LTE_SMS_MSG_MAX_LEN];
} lte_sms_request_t;

static QueueHandle_t s_sms_queue;
static SemaphoreHandle_t s_sms_busy_mutex;
static SemaphoreHandle_t s_uart_mutex;
static TaskHandle_t s_sms_worker_task;
static volatile bool s_modem_ready = false;

static esp_err_t lte_gpio_init(void)
{
    gpio_config_t cfg = {
        .pin_bit_mask = (1ULL << PIN_LTE_POWERKEY) | (1ULL << PIN_LTE_RESET),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };

    esp_err_t err = gpio_config(&cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "LTE control gpio_config failed: %s", esp_err_to_name(err));
        return err;
    }

    gpio_set_level(PIN_LTE_POWERKEY, 0);
    gpio_set_level(PIN_LTE_RESET, 0);
    return ESP_OK;
}

void lte_modem_set_ready(bool ready)
{
    s_modem_ready = ready;
    ESP_LOGI(TAG, "modem SMS ready: %s", ready ? "yes" : "no");
}

bool lte_modem_is_ready(void)
{
    return s_modem_ready;
}

static esp_err_t lte_modem_read_response_ex(char *rx_buf, size_t rx_buf_size, int *out_len, int timeout_ms)
{
    int total_len = 0;
    TickType_t start = xTaskGetTickCount();
    TickType_t timeout_ticks = pdMS_TO_TICKS(timeout_ms);

    while ((xTaskGetTickCount() - start) < timeout_ticks) {
        int len = uart_read_bytes(LTE_UART_PORT,
                                  (uint8_t *)rx_buf + total_len,
                                  rx_buf_size - 1 - total_len,
                                  pdMS_TO_TICKS(100));
        if (len > 0) {
            total_len += len;
            if (total_len >= (int)rx_buf_size - 1) {
                break;
            }
        }
    }

    if (total_len > 0) {
        rx_buf[total_len] = '\0';
        if (out_len != NULL) {
            *out_len = total_len;
        }
        return ESP_OK;
    }

    if (out_len != NULL) {
        *out_len = 0;
    }
    return ESP_ERR_TIMEOUT;
}

static bool lte_response_contains_ok(const char *response)
{
    return response != NULL && strstr(response, "OK") != NULL;
}

static bool lte_response_contains_error(const char *response)
{
    return response != NULL &&
           (strstr(response, "ERROR") != NULL ||
            strstr(response, "+CMS ERROR") != NULL ||
            strstr(response, "+CME ERROR") != NULL);
}

static bool lte_message_needs_ucs2(const char *message)
{
    const uint8_t *src = (const uint8_t *)message;
    while (*src != '\0') {
        if (*src > 0x7F) {
            return true;
        }
        src++;
    }
    return false;
}

static int lte_sms_toda_value(const char *phone_number)
{
    /* mss01: TODA=145 for +international, TODA=129 for local digits */
    if (phone_number != NULL && phone_number[0] == '+') {
        return 145;
    }
    return 129;
}

static esp_err_t lte_modem_wait_for_tokens_locked(const char *tokens[], size_t token_count,
                                                  char *rx_buf, size_t rx_buf_size,
                                                  int *out_len, int timeout_ms)
{
    int total_len = 0;
    TickType_t start = xTaskGetTickCount();
    TickType_t timeout_ticks = pdMS_TO_TICKS(timeout_ms);

    rx_buf[0] = '\0';
    if (out_len != NULL) {
        *out_len = 0;
    }

    while ((xTaskGetTickCount() - start) < timeout_ticks) {
        uint8_t byte = 0;
        int len = uart_read_bytes(LTE_UART_PORT, &byte, 1, pdMS_TO_TICKS(200));
        if (len <= 0) {
            continue;
        }

        if (total_len < (int)rx_buf_size - 1) {
            rx_buf[total_len++] = (char)byte;
            rx_buf[total_len] = '\0';
        }

        for (size_t i = 0; i < token_count; i++) {
            if (strstr(rx_buf, tokens[i]) != NULL) {
                if (out_len != NULL) {
                    *out_len = total_len;
                }
                return ESP_OK;
            }
        }
    }

    if (out_len != NULL) {
        *out_len = total_len;
    }
    return ESP_ERR_TIMEOUT;
}

static esp_err_t lte_modem_send_raw(const char *data, size_t len)
{
    int written = uart_write_bytes(LTE_UART_PORT, data, len);
    if (written < 0 || (size_t)written != len) {
        return ESP_FAIL;
    }
    return ESP_OK;
}

static esp_err_t lte_modem_send_command_locked(const char *cmd, const char *log_label, int timeout_ms)
{
    char rx_buf[512];
    int response_len = 0;

    uart_flush_input(LTE_UART_PORT);

    esp_err_t err = lte_modem_send_raw(cmd, strlen(cmd));
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "%s: send failed", log_label);
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "%s: sent %s", log_label, cmd);

    err = lte_modem_read_response_ex(rx_buf, sizeof(rx_buf), &response_len, timeout_ms);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "%s: response (%d bytes): %s", log_label, response_len, rx_buf);
        if (lte_response_contains_ok(rx_buf)) {
            return ESP_OK;
        }
        if (lte_response_contains_error(rx_buf)) {
            ESP_LOGE(TAG, "%s: modem returned ERROR", log_label);
            return ESP_FAIL;
        }
        ESP_LOGW(TAG, "%s: response received but OK not found", log_label);
        return ESP_FAIL;
    }

    ESP_LOGW(TAG, "%s: no response within %d ms", log_label, timeout_ms);
    return ESP_ERR_TIMEOUT;
}

static esp_err_t lte_modem_send_at_cr_locked(const char *cmd_body, const char *log_label, int timeout_ms)
{
    char cmd[160];
    int written = snprintf(cmd, sizeof(cmd), "%s\r", cmd_body);
    if (written <= 0 || written >= (int)sizeof(cmd)) {
        return ESP_ERR_INVALID_ARG;
    }
    return lte_modem_send_command_locked(cmd, log_label, timeout_ms);
}

static esp_err_t lte_modem_send_command(const char *cmd, const char *log_label)
{
    esp_err_t err;

    if (xSemaphoreTake(s_uart_mutex, pdMS_TO_TICKS(5000)) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }

    err = lte_modem_send_command_locked(cmd, log_label, LTE_AT_RESPONSE_TIMEOUT_MS);
    xSemaphoreGive(s_uart_mutex);
    return err;
}

static esp_err_t utf8_to_ucs2_hex(const char *utf8, char *hex_out, size_t hex_out_size)
{
    size_t hex_len = 0;
    const uint8_t *src = (const uint8_t *)utf8;

    hex_out[0] = '\0';

    while (*src != '\0') {
        uint32_t codepoint = 0;
        uint8_t b0 = src[0];

        if ((b0 & 0x80U) == 0U) {
            codepoint = b0;
            src += 1;
        } else if ((b0 & 0xE0U) == 0xC0U && src[1] != '\0') {
            codepoint = ((uint32_t)(b0 & 0x1FU) << 6) | (uint32_t)(src[1] & 0x3FU);
            src += 2;
        } else if ((b0 & 0xF0U) == 0xE0U && src[1] != '\0' && src[2] != '\0') {
            codepoint = ((uint32_t)(b0 & 0x0FU) << 12) |
                        ((uint32_t)(src[1] & 0x3FU) << 6) |
                        (uint32_t)(src[2] & 0x3FU);
            src += 3;
        } else {
            ESP_LOGE(TAG, "invalid UTF-8 sequence");
            return ESP_ERR_INVALID_ARG;
        }

        if (codepoint > 0xFFFFU) {
            ESP_LOGE(TAG, "codepoint U+%04lX not supported in UCS2", (unsigned long)codepoint);
            return ESP_ERR_NOT_SUPPORTED;
        }

        if (hex_len + 4 >= hex_out_size) {
            ESP_LOGE(TAG, "UCS2 hex buffer overflow");
            return ESP_ERR_NO_MEM;
        }

        int written = snprintf(hex_out + hex_len, hex_out_size - hex_len, "%04X", (unsigned int)codepoint);
        if (written != 4) {
            return ESP_FAIL;
        }
        hex_len += 4;
    }

    return ESP_OK;
}

static esp_err_t lte_modem_send_sms_blocking(const char *phone_number, const char *message)
{
    char phone_hex[64];
    char message_hex[LTE_UCS2_HEX_MAX_LEN];
    char payload[LTE_UCS2_HEX_MAX_LEN + 4];
    char cmgs_cmd[128];
    char rx_buf[512];
    int response_len = 0;
    esp_err_t err;
    bool use_ucs2 = false;
    int toda = 129;
    static const char *prompt_tokens[] = {">"};
    static const char *final_tokens[] = {"OK", "ERROR", "+CMS ERROR", "+CME ERROR"};

    if (phone_number == NULL || message == NULL || phone_number[0] == '\0' || message[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }

    if (!s_modem_ready) {
        ESP_LOGW(TAG, "sms send skipped: modem not ready");
        return ESP_ERR_INVALID_STATE;
    }

    if (xSemaphoreTake(s_uart_mutex, pdMS_TO_TICKS(60000)) != pdTRUE) {
        ESP_LOGE(TAG, "sms send failed: uart mutex timeout");
        return ESP_ERR_TIMEOUT;
    }

    ESP_LOGI(TAG, "sms send start: %s", phone_number);
    ESP_LOGI(TAG, "sms phone local: %s", phone_number);

    /* mss01/main.py send_sms() sequence */
    err = lte_modem_send_at_cr_locked("ATE0", "ATE0", LTE_AT_RESPONSE_TIMEOUT_MS);
    if (err != ESP_OK) {
        goto done;
    }

    err = lte_modem_send_at_cr_locked("AT+CMEE=2", "AT+CMEE", LTE_AT_RESPONSE_TIMEOUT_MS);
    if (err != ESP_OK) {
        goto done;
    }

    err = lte_modem_send_at_cr_locked("AT+CMGF=1", "AT+CMGF", LTE_AT_RESPONSE_TIMEOUT_MS);
    if (err != ESP_OK) {
        goto done;
    }

    use_ucs2 = lte_message_needs_ucs2(message);
    toda = lte_sms_toda_value(phone_number);
    ESP_LOGI(TAG, "sms mode: %s, TODA=%d", use_ucs2 ? "UCS2" : "GSM", toda);

    if (use_ucs2) {
        err = lte_modem_send_at_cr_locked("AT+CSCS=\"UCS2\"", "AT+CSCS", LTE_AT_RESPONSE_TIMEOUT_MS);
        if (err != ESP_OK) {
            goto done;
        }

        err = lte_modem_send_at_cr_locked("AT+CSMP=17,167,0,8", "AT+CSMP", LTE_AT_RESPONSE_TIMEOUT_MS);
        if (err != ESP_OK) {
            goto done;
        }

        err = utf8_to_ucs2_hex(phone_number, phone_hex, sizeof(phone_hex));
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "phone UCS2 encode failed");
            goto done;
        }

        err = utf8_to_ucs2_hex(message, message_hex, sizeof(message_hex));
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "message UCS2 encode failed");
            goto done;
        }
    } else {
        err = lte_modem_send_at_cr_locked("AT+CSCS=\"GSM\"", "AT+CSCS", LTE_AT_RESPONSE_TIMEOUT_MS);
        if (err != ESP_OK) {
            goto done;
        }

        err = lte_modem_send_at_cr_locked("AT+CSMP=17,167,0,0", "AT+CSMP", LTE_AT_RESPONSE_TIMEOUT_MS);
        if (err != ESP_OK) {
            goto done;
        }

        strncpy(phone_hex, phone_number, sizeof(phone_hex) - 1);
        phone_hex[sizeof(phone_hex) - 1] = '\0';
        strncpy(message_hex, message, sizeof(message_hex) - 1);
        message_hex[sizeof(message_hex) - 1] = '\0';
    }

    err = lte_modem_send_at_cr_locked("AT+CSCA?", "AT+CSCA", LTE_AT_RESPONSE_TIMEOUT_MS);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "AT+CSCA? failed, continuing");
    }

    ESP_LOGI(TAG, "sms UCS2 phone hex: %s", phone_hex);
    ESP_LOGI(TAG, "sms payload hex len: %d", (int)strlen(message_hex));

    snprintf(cmgs_cmd, sizeof(cmgs_cmd), "AT+CMGS=\"%s\",%d\r", phone_hex, toda);
    uart_flush_input(LTE_UART_PORT);

    err = lte_modem_send_raw(cmgs_cmd, strlen(cmgs_cmd));
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "AT+CMGS send failed");
        goto done;
    }

    ESP_LOGI(TAG, "AT+CMGS sent, waiting for > prompt (up to %d ms)", LTE_SMS_PROMPT_TIMEOUT_MS);

    err = lte_modem_wait_for_tokens_locked(prompt_tokens, 1, rx_buf, sizeof(rx_buf),
                                           &response_len, LTE_SMS_PROMPT_TIMEOUT_MS);
    if (err != ESP_OK) {
        if (response_len > 0) {
            ESP_LOGE(TAG, "SMS > prompt timeout, uart data: %s", rx_buf);
        } else {
            ESP_LOGE(TAG, "SMS > prompt timeout, no uart data");
        }
        goto done;
    }

    ESP_LOGI(TAG, "SMS > prompt received");

    ESP_LOGI(TAG, "sms body send start");
    ESP_LOGI(TAG, "sms body hex: %s", message_hex);

    /* mss01: message + Ctrl+Z in a single UART write, no CR/LF */
    snprintf(payload, sizeof(payload), "%s", message_hex);
    size_t payload_len = strlen(payload);
    if (payload_len + 1 >= sizeof(payload)) {
        ESP_LOGE(TAG, "sms payload buffer overflow");
        err = ESP_ERR_NO_MEM;
        goto done;
    }
    payload[payload_len] = 0x1A;
    payload_len += 1;

    err = lte_modem_send_raw(payload, payload_len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "sms send failed: payload uart write error");
        goto done;
    }

    ESP_LOGI(TAG, "sms body sent");
    ESP_LOGI(TAG, "sms ctrl-z sent");
    ESP_LOGI(TAG, "sms waiting final response (up to %d ms)", LTE_SMS_CMGS_TIMEOUT_MS);

    err = lte_modem_wait_for_tokens_locked(final_tokens, 4, rx_buf, sizeof(rx_buf),
                                           &response_len, LTE_SMS_CMGS_TIMEOUT_MS);
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "sms final response: %s", rx_buf);

        if (strstr(rx_buf, "+CMGS:") != NULL) {
            ESP_LOGI(TAG, "sms final response contains +CMGS");
        }

        if (strstr(rx_buf, "+CMS ERROR") != NULL || strstr(rx_buf, "+CME ERROR") != NULL) {
            ESP_LOGE(TAG, "sms send failed: modem CMS/CME ERROR");
            err = ESP_FAIL;
            goto done;
        }

        if (lte_response_contains_ok(rx_buf)) {
            ESP_LOGI(TAG, "sms send success");
            err = ESP_OK;
            goto done;
        }

        if (strstr(rx_buf, "ERROR") != NULL) {
            ESP_LOGE(TAG, "sms send failed: modem ERROR");
            err = ESP_FAIL;
            goto done;
        }
    } else if (response_len > 0) {
        ESP_LOGI(TAG, "sms final response (partial): %s", rx_buf);
    }

    ESP_LOGE(TAG, "sms final response timeout");
    ESP_LOGE(TAG, "sms send failed: %s", esp_err_to_name(err));

done:
    xSemaphoreGive(s_uart_mutex);
    return err;
}

static void lte_sms_worker_task(void *arg)
{
    (void)arg;
    lte_sms_request_t request;

    while (true) {
        if (xQueueReceive(s_sms_queue, &request, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        esp_err_t err = lte_modem_send_sms_blocking(request.phone, request.message);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "sms send failed: %s", esp_err_to_name(err));
        }

        xSemaphoreGive(s_sms_busy_mutex);
    }
}

static esp_err_t lte_sms_worker_init(void)
{
    s_sms_queue = xQueueCreate(LTE_SMS_QUEUE_LEN, sizeof(lte_sms_request_t));
    if (s_sms_queue == NULL) {
        ESP_LOGE(TAG, "failed to create SMS queue");
        return ESP_ERR_NO_MEM;
    }

    s_sms_busy_mutex = xSemaphoreCreateMutex();
    if (s_sms_busy_mutex == NULL) {
        ESP_LOGE(TAG, "failed to create SMS mutex");
        return ESP_ERR_NO_MEM;
    }

    s_uart_mutex = xSemaphoreCreateMutex();
    if (s_uart_mutex == NULL) {
        ESP_LOGE(TAG, "failed to create UART mutex");
        return ESP_ERR_NO_MEM;
    }

    BaseType_t ok = xTaskCreate(
        lte_sms_worker_task,
        "lte_sms",
        8192,
        NULL,
        5,
        &s_sms_worker_task);

    if (ok != pdPASS) {
        ESP_LOGE(TAG, "failed to create SMS worker task");
        return ESP_FAIL;
    }

    ESP_LOGI(TAG, "SMS worker task started");
    return ESP_OK;
}

esp_err_t lte_modem_init(void)
{
    esp_err_t err = lte_gpio_init();
    if (err != ESP_OK) {
        return err;
    }

    uart_config_t uart_cfg = {
        .baud_rate = LTE_UART_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };

    err = uart_driver_install(LTE_UART_PORT, LTE_UART_RX_BUF_SIZE * 2, 0, 0, NULL, 0);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "uart_driver_install failed: %s", esp_err_to_name(err));
        return err;
    }

    err = uart_param_config(LTE_UART_PORT, &uart_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "uart_param_config failed: %s", esp_err_to_name(err));
        return err;
    }

    err = uart_set_pin(LTE_UART_PORT, PIN_LTE_TX, PIN_LTE_RX,
                       UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "uart_set_pin failed: %s", esp_err_to_name(err));
        return err;
    }

    uart_flush_input(LTE_UART_PORT);
    ESP_LOGI(TAG, "UART%d ready: TX=GPIO%d, RX=GPIO%d, %d bps, 8N1, no flow control",
             LTE_UART_PORT, PIN_LTE_TX, PIN_LTE_RX, LTE_UART_BAUD);

    return lte_sms_worker_init();
}

esp_err_t lte_modem_prepare_for_sms(void)
{
    esp_err_t err;

    err = lte_modem_send_command("AT\r\n", "AT");
    if (err != ESP_OK) {
        return err;
    }

    err = lte_modem_send_command("AT*CPIN?\r\n", "AT*CPIN");
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "CPIN check failed, SMS may not work");
    }

    err = lte_modem_send_command("AT+CSQ\r\n", "AT+CSQ");
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "CSQ check failed");
    }

    err = lte_modem_send_command("AT*REGSTS?\r\n", "AT*REGSTS");
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "REGSTS check failed, SMS may not work");
    }

    return ESP_OK;
}

esp_err_t lte_modem_send_sms(const char *phone_number, const char *message)
{
    if (s_sms_queue == NULL || phone_number == NULL || message == NULL) {
        return ESP_ERR_INVALID_STATE;
    }

    if (!s_modem_ready) {
        ESP_LOGW(TAG, "sms queue rejected: modem not ready");
        return ESP_ERR_INVALID_STATE;
    }

    if (xSemaphoreTake(s_sms_busy_mutex, 0) != pdTRUE) {
        ESP_LOGW(TAG, "sms busy, skip duplicate request");
        return ESP_ERR_INVALID_STATE;
    }

    lte_sms_request_t request = {0};
    strncpy(request.phone, phone_number, sizeof(request.phone) - 1);
    strncpy(request.message, message, sizeof(request.message) - 1);

    if (xQueueSend(s_sms_queue, &request, 0) != pdTRUE) {
        xSemaphoreGive(s_sms_busy_mutex);
        ESP_LOGW(TAG, "sms queue full");
        return ESP_ERR_NO_MEM;
    }

    return ESP_OK;
}

esp_err_t lte_modem_powerkey_pulse(void)
{
    ESP_LOGI(TAG, "LTE POWERKEY pulse: GPIO%d HIGH %dms", PIN_LTE_POWERKEY, LTE_CTRL_PULSE_MS);
    gpio_set_level(PIN_LTE_POWERKEY, 1);
    vTaskDelay(pdMS_TO_TICKS(LTE_CTRL_PULSE_MS));
    gpio_set_level(PIN_LTE_POWERKEY, 0);
    ESP_LOGI(TAG, "LTE POWERKEY pulse: GPIO%d LOW", PIN_LTE_POWERKEY);
    return ESP_OK;
}

esp_err_t lte_modem_reset_pulse(void)
{
    ESP_LOGI(TAG, "LTE RESET pulse: GPIO%d HIGH %dms", PIN_LTE_RESET, LTE_CTRL_PULSE_MS);
    gpio_set_level(PIN_LTE_RESET, 1);
    vTaskDelay(pdMS_TO_TICKS(LTE_CTRL_PULSE_MS));
    gpio_set_level(PIN_LTE_RESET, 0);
    ESP_LOGI(TAG, "LTE RESET pulse: GPIO%d LOW", PIN_LTE_RESET);
    return ESP_OK;
}

esp_err_t lte_modem_send_at_test(void)
{
    const char *at_cmd = "AT\r\n";
    esp_err_t err = ESP_ERR_TIMEOUT;

    if (xSemaphoreTake(s_uart_mutex, pdMS_TO_TICKS(5000)) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }

    for (int attempt = 1; attempt <= LTE_AT_RETRY_MAX; attempt++) {
        char rx_buf[256];
        int response_len = 0;

        uart_flush_input(LTE_UART_PORT);

        int written = uart_write_bytes(LTE_UART_PORT, at_cmd, strlen(at_cmd));
        if (written < 0) {
            ESP_LOGE(TAG, "failed to send AT command");
            err = ESP_FAIL;
            break;
        }

        ESP_LOGI(TAG, "AT test attempt %d/%d: sent AT\\r\\n", attempt, LTE_AT_RETRY_MAX);

        err = lte_modem_read_response_ex(rx_buf, sizeof(rx_buf), &response_len, LTE_AT_RESPONSE_TIMEOUT_MS);
        if (err == ESP_OK) {
            ESP_LOGI(TAG, "AT response (%d bytes): %s", response_len, rx_buf);
            if (lte_response_contains_ok(rx_buf)) {
                ESP_LOGI(TAG, "LTE modem AT test success");
                err = ESP_OK;
                break;
            }
        }

        if (attempt < LTE_AT_RETRY_MAX) {
            vTaskDelay(pdMS_TO_TICKS(LTE_AT_RETRY_INTERVAL_MS));
        }
    }

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "LTE modem AT response timeout");
    }

    xSemaphoreGive(s_uart_mutex);
    return err;
}

esp_err_t lte_modem_cmd_ati(void)
{
    return lte_modem_send_command("ATI\r\n", "ATI");
}

esp_err_t lte_modem_cmd_csq(void)
{
    return lte_modem_send_command("AT+CSQ\r\n", "AT+CSQ");
}

esp_err_t lte_modem_cmd_antlvl(void)
{
    return lte_modem_send_command("AT*ANTLVL?\r\n", "AT*ANTLVL?");
}

esp_err_t lte_modem_cmd_regsts(void)
{
    return lte_modem_send_command("AT*REGSTS?\r\n", "AT*REGSTS?");
}

esp_err_t lte_modem_cmd_stat(void)
{
    return lte_modem_send_command("AT$$STAT?\r\n", "AT$$STAT?");
}

esp_err_t lte_modem_cmd_cpin(void)
{
    return lte_modem_send_command("AT*CPIN?\r\n", "AT*CPIN?");
}
