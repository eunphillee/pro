#pragma once

#include <stddef.h>

#include "esp_err.h"

#define ALARM_MSG_COUNT         7
#define ALARM_PHONE_COUNT       10
#define ALARM_MSG_MAX_BYTES     80
#define ALARM_MSG_MAX_LEN       (ALARM_MSG_MAX_BYTES + 1)
#define ALARM_PHONE_MAX_LEN     13
#define ALARM_CONFIG_JSON_MAX   2048

esp_err_t alarm_config_init(void);
const char *alarm_config_get_message(int index);
const char *alarm_config_get_phone(int index);
esp_err_t alarm_config_set_message(int index, const char *msg);
esp_err_t alarm_config_set_phone(int index, const char *phone);
esp_err_t alarm_config_restore_defaults(void);
esp_err_t alarm_config_save_to_nvs(void);
int alarm_config_to_json(char *buf, size_t buf_size);
esp_err_t alarm_config_from_json(const char *json, size_t json_len);
const char *alarm_config_get_status(void);
void alarm_config_set_status(const char *status);
int alarm_config_count_configured_phones(void);
