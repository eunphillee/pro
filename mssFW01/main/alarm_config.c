#include "alarm_config.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"
#include "nvs_flash.h"

static const char *TAG = "alarm_cfg";
static const char *NVS_NAMESPACE = "alarm_cfg";

static char s_messages[ALARM_MSG_COUNT][ALARM_MSG_MAX_LEN];
static char s_phones[ALARM_PHONE_COUNT][ALARM_PHONE_MAX_LEN];
static char s_status[32] = "OK";
static SemaphoreHandle_t s_mutex;

static const char *DEFAULT_MESSAGES[ALARM_MSG_COUNT] = {
    "침수위험(신천IC 배수펌프 #4,5)",
    "대피하세요(신천IC 배수펌프 #4,5)",
    "가스 이상 감지(O2)",
    "가스 이상 감지(CO)",
    "가스 이상 감지(H2S)",
    "가스 이상 감지(LEL)",
    "가스 이상 감지(CO2)",
};

static const char *DEFAULT_PHONE_1 = "01026844484";

static bool alarm_config_utf8_len_ok(const char *text)
{
    size_t len = 0;

    if (text == NULL) {
        return false;
    }

    len = strlen(text);
    return len <= ALARM_MSG_MAX_BYTES;
}

static void alarm_config_copy_message(int index, const char *msg)
{
    if (index < 0 || index >= ALARM_MSG_COUNT || msg == NULL) {
        return;
    }

    strncpy(s_messages[index], msg, ALARM_MSG_MAX_LEN - 1);
    s_messages[index][ALARM_MSG_MAX_LEN - 1] = '\0';
}

static void alarm_config_copy_phone(int index, const char *phone)
{
    if (index < 0 || index >= ALARM_PHONE_COUNT || phone == NULL) {
        return;
    }

    strncpy(s_phones[index], phone, ALARM_PHONE_MAX_LEN - 1);
    s_phones[index][ALARM_PHONE_MAX_LEN - 1] = '\0';
}

esp_err_t alarm_config_restore_defaults(void)
{
    int i;

    for (i = 0; i < ALARM_MSG_COUNT; i++) {
        alarm_config_copy_message(i, DEFAULT_MESSAGES[i]);
    }

    alarm_config_copy_phone(0, DEFAULT_PHONE_1);
    for (i = 1; i < ALARM_PHONE_COUNT; i++) {
        s_phones[i][0] = '\0';
    }

    return ESP_OK;
}

static esp_err_t alarm_config_load_key_str(nvs_handle_t handle, const char *key, char *dest, size_t dest_len)
{
    size_t required = dest_len;
    esp_err_t err = nvs_get_str(handle, key, dest, &required);

    if (err == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_ERR_NOT_FOUND;
    }

    return err;
}

esp_err_t alarm_config_load_from_nvs(void)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READONLY, &handle);
    bool any_found = false;

    if (err != ESP_OK) {
        return err;
    }

    for (int i = 0; i < ALARM_MSG_COUNT; i++) {
        char key[16];
        snprintf(key, sizeof(key), "alarm_msg_%d", i + 1);
        err = alarm_config_load_key_str(handle, key, s_messages[i], sizeof(s_messages[i]));
        if (err == ESP_OK) {
            any_found = true;
        }
    }

    for (int i = 0; i < ALARM_PHONE_COUNT; i++) {
        char key[16];
        snprintf(key, sizeof(key), "phone_%d", i + 1);
        err = alarm_config_load_key_str(handle, key, s_phones[i], sizeof(s_phones[i]));
        if (err == ESP_OK) {
            any_found = true;
        }
    }

    nvs_close(handle);

    return any_found ? ESP_OK : ESP_ERR_NOT_FOUND;
}

esp_err_t alarm_config_save_to_nvs(void)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &handle);

    if (err != ESP_OK) {
        return err;
    }

    for (int i = 0; i < ALARM_MSG_COUNT; i++) {
        char key[16];
        snprintf(key, sizeof(key), "alarm_msg_%d", i + 1);
        err = nvs_set_str(handle, key, s_messages[i]);
        if (err != ESP_OK) {
            nvs_close(handle);
            return err;
        }
    }

    for (int i = 0; i < ALARM_PHONE_COUNT; i++) {
        char key[16];
        snprintf(key, sizeof(key), "phone_%d", i + 1);
        err = nvs_set_str(handle, key, s_phones[i]);
        if (err != ESP_OK) {
            nvs_close(handle);
            return err;
        }
    }

    err = nvs_commit(handle);
    nvs_close(handle);
    return err;
}

esp_err_t alarm_config_init(void)
{
    s_mutex = xSemaphoreCreateMutex();
    if (s_mutex == NULL) {
        return ESP_ERR_NO_MEM;
    }

    alarm_config_restore_defaults();

    esp_err_t err = alarm_config_load_from_nvs();
    if (err == ESP_OK) {
        ESP_LOGI(TAG, "loaded alarm config from NVS");
    } else {
        ESP_LOGI(TAG, "using default alarm config");
        alarm_config_save_to_nvs();
    }

    return ESP_OK;
}

const char *alarm_config_get_message(int index)
{
    if (index < 0 || index >= ALARM_MSG_COUNT) {
        return "";
    }

    return s_messages[index];
}

const char *alarm_config_get_phone(int index)
{
    if (index < 0 || index >= ALARM_PHONE_COUNT) {
        return "";
    }

    return s_phones[index];
}

esp_err_t alarm_config_set_message(int index, const char *msg)
{
    if (index < 0 || index >= ALARM_MSG_COUNT || msg == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    if (!alarm_config_utf8_len_ok(msg)) {
        return ESP_ERR_INVALID_SIZE;
    }

    if (xSemaphoreTake(s_mutex, pdMS_TO_TICKS(1000)) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }

    alarm_config_copy_message(index, msg);
    xSemaphoreGive(s_mutex);
    return ESP_OK;
}

esp_err_t alarm_config_set_phone(int index, const char *phone)
{
    if (index < 0 || index >= ALARM_PHONE_COUNT || phone == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    if (strlen(phone) > ALARM_PHONE_MAX_LEN - 1) {
        return ESP_ERR_INVALID_SIZE;
    }

    if (xSemaphoreTake(s_mutex, pdMS_TO_TICKS(1000)) != pdTRUE) {
        return ESP_ERR_TIMEOUT;
    }

    alarm_config_copy_phone(index, phone);
    xSemaphoreGive(s_mutex);
    return ESP_OK;
}

int alarm_config_to_json(char *buf, size_t buf_size)
{
    cJSON *root = cJSON_CreateObject();
    cJSON *messages = cJSON_CreateArray();
    cJSON *phones = cJSON_CreateArray();
    char *printed = NULL;
    int written = 0;

    if (root == NULL || messages == NULL || phones == NULL) {
        cJSON_Delete(root);
        cJSON_Delete(messages);
        cJSON_Delete(phones);
        return -1;
    }

    if (xSemaphoreTake(s_mutex, pdMS_TO_TICKS(1000)) != pdTRUE) {
        cJSON_Delete(root);
        return -1;
    }

    for (int i = 0; i < ALARM_MSG_COUNT; i++) {
        cJSON_AddItemToArray(messages, cJSON_CreateString(s_messages[i]));
    }
    for (int i = 0; i < ALARM_PHONE_COUNT; i++) {
        cJSON_AddItemToArray(phones, cJSON_CreateString(s_phones[i]));
    }

    xSemaphoreGive(s_mutex);

    cJSON_AddItemToObject(root, "messages", messages);
    cJSON_AddItemToObject(root, "phones", phones);

    printed = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);

    if (printed == NULL) {
        return -1;
    }

    written = snprintf(buf, buf_size, "%s", printed);
    cJSON_free(printed);
    return written;
}

esp_err_t alarm_config_from_json(const char *json, size_t json_len)
{
    char *copy = NULL;
    cJSON *root = NULL;
    cJSON *messages = NULL;
    cJSON *phones = NULL;
    esp_err_t result = ESP_OK;

    if (json == NULL || json_len == 0 || json_len >= ALARM_CONFIG_JSON_MAX) {
        return ESP_ERR_INVALID_SIZE;
    }

    copy = malloc(json_len + 1);
    if (copy == NULL) {
        return ESP_ERR_NO_MEM;
    }

    memcpy(copy, json, json_len);
    copy[json_len] = '\0';

    root = cJSON_Parse(copy);
    free(copy);

    if (root == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    messages = cJSON_GetObjectItem(root, "messages");
    phones = cJSON_GetObjectItem(root, "phones");

    if (!cJSON_IsArray(messages) || !cJSON_IsArray(phones)) {
        cJSON_Delete(root);
        return ESP_ERR_INVALID_ARG;
    }

    if (cJSON_GetArraySize(messages) != ALARM_MSG_COUNT ||
        cJSON_GetArraySize(phones) != ALARM_PHONE_COUNT) {
        cJSON_Delete(root);
        return ESP_ERR_INVALID_SIZE;
    }

    if (xSemaphoreTake(s_mutex, pdMS_TO_TICKS(2000)) != pdTRUE) {
        cJSON_Delete(root);
        return ESP_ERR_TIMEOUT;
    }

    for (int i = 0; i < ALARM_MSG_COUNT; i++) {
        cJSON *item = cJSON_GetArrayItem(messages, i);
        if (!cJSON_IsString(item) || !alarm_config_utf8_len_ok(item->valuestring)) {
            result = ESP_ERR_INVALID_SIZE;
            break;
        }
        alarm_config_copy_message(i, item->valuestring);
    }

    if (result == ESP_OK) {
        for (int i = 0; i < ALARM_PHONE_COUNT; i++) {
            cJSON *item = cJSON_GetArrayItem(phones, i);
            if (!cJSON_IsString(item)) {
                result = ESP_ERR_INVALID_ARG;
                break;
            }
            if (strlen(item->valuestring) > ALARM_PHONE_MAX_LEN - 1) {
                result = ESP_ERR_INVALID_SIZE;
                break;
            }
            alarm_config_copy_phone(i, item->valuestring);
        }
    }

    xSemaphoreGive(s_mutex);
    cJSON_Delete(root);
    return result;
}

const char *alarm_config_get_status(void)
{
    return s_status;
}

void alarm_config_set_status(const char *status)
{
    if (status == NULL) {
        return;
    }

    strncpy(s_status, status, sizeof(s_status) - 1);
    s_status[sizeof(s_status) - 1] = '\0';
}

int alarm_config_count_configured_phones(void)
{
    int count = 0;

    if (xSemaphoreTake(s_mutex, pdMS_TO_TICKS(1000)) != pdTRUE) {
        return 0;
    }

    for (int i = 0; i < ALARM_PHONE_COUNT; i++) {
        if (s_phones[i][0] != '\0') {
            count++;
        }
    }

    xSemaphoreGive(s_mutex);
    return count;
}
