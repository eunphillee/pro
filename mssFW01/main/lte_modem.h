#pragma once

#include <stdbool.h>

#include "esp_err.h"

esp_err_t lte_modem_init(void);
esp_err_t lte_modem_powerkey_pulse(void);
esp_err_t lte_modem_reset_pulse(void);

/** Boot-time AT test with retry (max 10 attempts, 1s interval). */
esp_err_t lte_modem_send_at_test(void);

/** Check SIM/network before SMS; call after AT test succeeds. */
esp_err_t lte_modem_prepare_for_sms(void);

/** Mark modem ready for SMS dispatch from input module. */
void lte_modem_set_ready(bool ready);
bool lte_modem_is_ready(void);

/** Queue an SMS (non-blocking; worker task performs AT send). */
esp_err_t lte_modem_send_sms(const char *phone_number, const char *message);

/** Extended AT commands (manual invocation; not called at boot). */
esp_err_t lte_modem_cmd_ati(void);
esp_err_t lte_modem_cmd_csq(void);
esp_err_t lte_modem_cmd_antlvl(void);
esp_err_t lte_modem_cmd_regsts(void);
esp_err_t lte_modem_cmd_stat(void);
esp_err_t lte_modem_cmd_cpin(void);
