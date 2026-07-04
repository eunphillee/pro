#pragma once

#include <stdbool.h>

#include "esp_err.h"

/** Raw GPIO levels and LOW-active interpreted ON/OFF state. */
typedef struct {
    int raw_in1;
    int raw_in2;
    int raw_in3;
    bool in1_on;
    bool in2_on;
    bool in3_on;
} input_state_t;

/**
 * Latest input event for logging and future LTE SMS dispatch.
 * Updated by the monitor task when input combination changes.
 */
typedef struct {
    input_state_t state;
    const char *message;
    bool valid;
} input_event_t;

esp_err_t input_init(void);
esp_err_t input_start_monitor(void);

/** Read GPIO and convert to LOW-active ON/OFF state. */
void input_read_state(input_state_t *out);

/** Select alarm message from interpreted input state. */
const char *input_get_alarm_message(bool in1_on, bool in2_on, bool in3_on);

/** Pointer to latest event (valid after first state change or initial read). */
const input_event_t *input_get_event(void);

/** Call after LTE modem is ready to flush any pending alarm SMS. */
void input_notify_modem_ready(void);
