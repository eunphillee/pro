#include "ble_config.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "alarm_config.h"
#include "esp_log.h"
#include "host/ble_gatt.h"
#include "host/ble_hs.h"
#include "host/ble_store.h"
#include "host/ble_uuid.h"
#include "host/util/util.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

static const char *TAG = "BLE";
static const char *DEVICE_NAME = "MSS03_ALARM";
static const char *SERVICE_UUID_STR = "0000A100-0000-1000-8000-00805F9B34FB";

void ble_store_config_init(void);

#define BLE_CONFIG_JSON_BUF_SIZE    ALARM_CONFIG_JSON_MAX
/* 0=return full config JSON from NVS; 1=return short READ_OK for Windows read test */
#define BLE_CONFIG_READ_SHORT_TEST  0
#define BLE_READ_SHORT_RESPONSE     "READ_OK"

static const ble_uuid16_t adv_uuid16 = BLE_UUID16_INIT(0xA100);

static const ble_uuid128_t svc_uuid =
    BLE_UUID128_INIT(0xfb, 0x34, 0x9b, 0x5f, 0x80, 0x00, 0x00, 0x80,
                     0x00, 0x10, 0x00, 0x00, 0x00, 0xa1, 0x00, 0x00);

static const ble_uuid128_t chr_read_uuid =
    BLE_UUID128_INIT(0xfb, 0x34, 0x9b, 0x5f, 0x80, 0x00, 0x00, 0x80,
                     0x00, 0x10, 0x00, 0x00, 0x01, 0xa1, 0x00, 0x00);

static const ble_uuid128_t chr_write_uuid =
    BLE_UUID128_INIT(0xfb, 0x34, 0x9b, 0x5f, 0x80, 0x00, 0x00, 0x80,
                     0x00, 0x10, 0x00, 0x00, 0x02, 0xa1, 0x00, 0x00);

static const ble_uuid128_t chr_status_uuid =
    BLE_UUID128_INIT(0xfb, 0x34, 0x9b, 0x5f, 0x80, 0x00, 0x00, 0x80,
                     0x00, 0x10, 0x00, 0x00, 0x03, 0xa1, 0x00, 0x00);

static uint16_t s_read_handle;
static uint16_t s_write_handle;
static uint16_t s_status_handle;
static uint8_t s_own_addr_type;
static bool s_adv_active;
static char s_write_buf[BLE_CONFIG_JSON_BUF_SIZE];
static char s_read_json_buf[BLE_CONFIG_JSON_BUF_SIZE];
static uint16_t s_write_len;

static int ble_config_gap_event(struct ble_gap_event *event, void *arg);
static int ble_config_gatt_access(uint16_t conn_handle, uint16_t attr_handle,
                                  struct ble_gatt_access_ctxt *ctxt, void *arg);
static int ble_config_read_access(uint16_t conn_handle, uint16_t attr_handle,
                                  struct ble_gatt_access_ctxt *ctxt, void *arg);
static int ble_config_write_access(uint16_t conn_handle, uint16_t attr_handle,
                                   struct ble_gatt_access_ctxt *ctxt, void *arg);

static const struct ble_gatt_svc_def gatt_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &svc_uuid.u,
        .characteristics = (struct ble_gatt_chr_def[]) {
            {
                .uuid = &chr_read_uuid.u,
                .access_cb = ble_config_read_access,
                .val_handle = &s_read_handle,
                .flags = BLE_GATT_CHR_F_READ,
            },
            {
                .uuid = &chr_write_uuid.u,
                .access_cb = ble_config_write_access,
                .val_handle = &s_write_handle,
                .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_NO_RSP,
            },
            {
                .uuid = &chr_status_uuid.u,
                .access_cb = ble_config_gatt_access,
                .val_handle = &s_status_handle,
                .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY,
            },
            { 0 },
        },
    },
    { 0 },
};

static void ble_config_log_addr(void)
{
    uint8_t addr[6] = {0};
    int rc = ble_hs_id_copy_addr(s_own_addr_type, addr, NULL);
    if (rc != 0) {
        ESP_LOGW(TAG, "address read failed: %d", rc);
        return;
    }
    ESP_LOGI(TAG, "public address: %02X:%02X:%02X:%02X:%02X:%02X",
             addr[5], addr[4], addr[3], addr[2], addr[1], addr[0]);
}

static const char *ble_gatt_op_str(int op)
{
    switch (op) {
    case BLE_GATT_ACCESS_OP_READ_CHR:
        return "READ_CHR";
    case BLE_GATT_ACCESS_OP_WRITE_CHR:
        return "WRITE_CHR";
    case BLE_GATT_ACCESS_OP_READ_DSC:
        return "READ_DSC";
    case BLE_GATT_ACCESS_OP_WRITE_DSC:
        return "WRITE_DSC";
    default:
        return "UNKNOWN";
    }
}

static void ble_config_notify_status(uint16_t conn_handle)
{
    const char *status = alarm_config_get_status();
    struct os_mbuf *om;
    int rc;

    om = ble_hs_mbuf_from_flat(status, (uint16_t)strlen(status));
    if (om == NULL) {
        ESP_LOGW(TAG, "notify: mbuf alloc failed");
        return;
    }

    rc = ble_gatts_notify_custom(conn_handle, s_status_handle, om);
    if (rc != 0) {
        ESP_LOGW(TAG, "notify failed: rc=%d status=%s", rc, status);
    } else {
        ESP_LOGI(TAG, "notify sent: %s", status);
    }
}

/* Dedicated callback for config write characteristic (0000a102). */
static int ble_config_write_access(uint16_t conn_handle, uint16_t attr_handle,
                                   struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    char uuid_str[BLE_UUID_STR_LEN];
    uint16_t om_len;
    int rc;

    ble_uuid_to_str(ctxt->chr->uuid, uuid_str);

    ESP_LOGI(TAG, "write received");
    ESP_LOGI(TAG, "uuid=%s", uuid_str);
    ESP_LOGI(TAG, "attr_handle=%u conn_handle=%u op=%s",
             attr_handle, conn_handle, ble_gatt_op_str(ctxt->op));

    om_len = OS_MBUF_PKTLEN(ctxt->om);
    ESP_LOGI(TAG, "len=%u", om_len);

    if (om_len == 0 || om_len >= sizeof(s_write_buf)) {
        ESP_LOGW(TAG, "write rejected: length out of range");
        alarm_config_set_status("ERROR_LENGTH");
        ble_config_notify_status(conn_handle);
        return 0;
    }

    rc = ble_hs_mbuf_to_flat(ctxt->om, s_write_buf, sizeof(s_write_buf) - 1, &s_write_len);
    if (rc != 0) {
        ESP_LOGW(TAG, "write flat copy failed: rc=%d", rc);
        alarm_config_set_status("ERROR_JSON");
        ble_config_notify_status(conn_handle);
        return 0;
    }

    s_write_buf[s_write_len] = '\0';
    ESP_LOGI(TAG, "data=%s", s_write_buf);

    esp_err_t err = alarm_config_from_json(s_write_buf, s_write_len);
    if (err != ESP_OK) {
        alarm_config_set_status(err == ESP_ERR_INVALID_SIZE ? "ERROR_LENGTH" : "ERROR_JSON");
        ESP_LOGW(TAG, "config parse failed: %s", esp_err_to_name(err));
        ble_config_notify_status(conn_handle);
        return 0;
    }
    ESP_LOGI(TAG, "config parse ok");

    err = alarm_config_save_to_nvs();
    if (err != ESP_OK) {
        alarm_config_set_status("ERROR_SAVE");
        ESP_LOGE(TAG, "config save failed: %s", esp_err_to_name(err));
        ble_config_notify_status(conn_handle);
        return 0;
    }

    alarm_config_set_status("OK");
    ESP_LOGI(TAG, "config saved");
    ble_config_notify_status(conn_handle);
    return 0;
}

/* Dedicated callback for config read characteristic (0000a101). */
static int ble_config_read_access(uint16_t conn_handle, uint16_t attr_handle,
                                  struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    char uuid_str[BLE_UUID_STR_LEN];
    const char *payload;
    uint16_t payload_len;
    int rc;

    ble_uuid_to_str(ctxt->chr->uuid, uuid_str);

    ESP_LOGI(TAG, "read callback entered");
    ESP_LOGI(TAG, "read uuid=%s", uuid_str);
    ESP_LOGI(TAG, "attr_handle=%u conn_handle=%u op=%s",
             attr_handle, conn_handle, ble_gatt_op_str(ctxt->op));

    if (ctxt->op != BLE_GATT_ACCESS_OP_READ_CHR) {
        ESP_LOGW(TAG, "read: unexpected op, returning 0");
        return 0;
    }

    ESP_LOGI(TAG, "config read requested");

#if BLE_CONFIG_READ_SHORT_TEST
    payload = BLE_READ_SHORT_RESPONSE;
    payload_len = (uint16_t)strlen(payload);
    ESP_LOGI(TAG, "read mode: short test response");
#else
    {
        int json_len = alarm_config_to_json(s_read_json_buf, sizeof(s_read_json_buf));
        if (json_len <= 0) {
            ESP_LOGW(TAG, "config json build failed, fallback READ_OK");
            payload = BLE_READ_SHORT_RESPONSE;
            payload_len = (uint16_t)strlen(payload);
        } else {
            payload = s_read_json_buf;
            payload_len = (uint16_t)json_len;
            ESP_LOGI(TAG, "read mode: full config json");
        }
    }
#endif

    ESP_LOGI(TAG, "config json length=%u", payload_len);
    if (payload_len > 0 && payload_len < 200) {
        ESP_LOGI(TAG, "config json=%.*s", payload_len, payload);
    } else if (payload_len > 0) {
        ESP_LOGI(TAG, "config json=%.120s...", payload);
    }
    rc = os_mbuf_append(ctxt->om, payload, payload_len);
    ESP_LOGI(TAG, "os_mbuf_append result=%d", rc);
#if BLE_CONFIG_READ_SHORT_TEST
    ESP_LOGI(TAG, "read return short test");
#else
    ESP_LOGI(TAG, "read return config json");
#endif
    return 0;
}

static int ble_config_gatt_access(uint16_t conn_handle, uint16_t attr_handle,
                                  struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    const ble_uuid_t *uuid = ctxt->chr->uuid;
    char uuid_str[BLE_UUID_STR_LEN];
    int rc;

    ble_uuid_to_str(uuid, uuid_str);

    ESP_LOGI(TAG, "access callback entered");
    ESP_LOGI(TAG, "attr_handle=%u conn_handle=%u", attr_handle, conn_handle);
    ESP_LOGI(TAG, "op=%d (%s)", ctxt->op, ble_gatt_op_str(ctxt->op));
    ESP_LOGI(TAG, "uuid=%s", uuid_str);

    if (ble_uuid_cmp(uuid, &chr_status_uuid.u) == 0) {
        if (ctxt->op != BLE_GATT_ACCESS_OP_READ_CHR) {
            return BLE_ATT_ERR_UNLIKELY;
        }

        const char *status = alarm_config_get_status();
        rc = os_mbuf_append(ctxt->om, status, (uint16_t)strlen(status));
        return rc == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
    }

    return BLE_ATT_ERR_UNLIKELY;
}

static void ble_config_advertise(void)
{
    struct ble_gap_adv_params adv_params;
    struct ble_hs_adv_fields fields;
    struct ble_hs_adv_fields rsp_fields;
    const char *name;
    int rc;

    if (s_adv_active) {
        ble_gap_adv_stop();
        s_adv_active = false;
    }

    name = ble_svc_gap_device_name();
    if (name == NULL || name[0] == '\0') {
        name = DEVICE_NAME;
    }

    /* Primary advertising: flags + Complete Local Name (+ 16-bit UUID for Windows) */
    memset(&fields, 0, sizeof(fields));
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    fields.name = (uint8_t *)name;
    fields.name_len = strlen(name);
    fields.name_is_complete = 1;
    fields.uuids16 = (ble_uuid16_t *)&adv_uuid16;
    fields.num_uuids16 = 1;
    fields.uuids16_is_complete = 1;

    rc = ble_gap_adv_set_fields(&fields);
    if (rc != 0) {
        ESP_LOGW(TAG, "adv with name+uuid16 failed: %d, retry name only", rc);
        memset(&fields, 0, sizeof(fields));
        fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
        fields.name = (uint8_t *)name;
        fields.name_len = strlen(name);
        fields.name_is_complete = 1;
        rc = ble_gap_adv_set_fields(&fields);
        if (rc != 0) {
            ESP_LOGE(TAG, "advertising data set failed: %d", rc);
            return;
        }
    }
    ESP_LOGI(TAG, "adv data includes complete local name");

    /* Scan response: Complete Local Name + 128-bit Service UUID */
    memset(&rsp_fields, 0, sizeof(rsp_fields));
    rsp_fields.name = (uint8_t *)name;
    rsp_fields.name_len = strlen(name);
    rsp_fields.name_is_complete = 1;
    rsp_fields.uuids128 = (ble_uuid128_t *)&svc_uuid;
    rsp_fields.num_uuids128 = 1;
    rsp_fields.uuids128_is_complete = 1;

    rc = ble_gap_adv_rsp_set_fields(&rsp_fields);
    if (rc != 0) {
        ESP_LOGW(TAG, "scan response name+uuid128 failed: %d, retry name only", rc);
        memset(&rsp_fields, 0, sizeof(rsp_fields));
        rsp_fields.name = (uint8_t *)name;
        rsp_fields.name_len = strlen(name);
        rsp_fields.name_is_complete = 1;
        rc = ble_gap_adv_rsp_set_fields(&rsp_fields);
        if (rc != 0) {
            ESP_LOGE(TAG, "scan response set failed: %d", rc);
            return;
        }
    }
    ESP_LOGI(TAG, "scan response includes complete local name");

    memset(&adv_params, 0, sizeof(adv_params));
    adv_params.conn_mode = BLE_GAP_CONN_MODE_UND;
    adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;

    rc = ble_gap_adv_start(s_own_addr_type, NULL, BLE_HS_FOREVER,
                           &adv_params, ble_config_gap_event, NULL);
    if (rc != 0) {
        ESP_LOGE(TAG, "advertising start failed: %d", rc);
        return;
    }

    s_adv_active = true;
    ESP_LOGI(TAG, "device name: %s", name);
    ESP_LOGI(TAG, "service uuid: %s", SERVICE_UUID_STR);
    ESP_LOGI(TAG, "advertising started");
}

static const char *ble_disconnect_reason_str(int reason)
{
    /* reason = BLE_HS_ERR_HCI_BASE(0x200) + HCI error code */
    if (reason < BLE_HS_ERR_HCI_BASE) {
        return "non-HCI";
    }
    switch (reason - BLE_HS_ERR_HCI_BASE) {
    case 0x05: return "Auth Failure";
    case 0x08: return "Conn Timeout";
    case 0x13: return "Remote User Terminated";
    case 0x16: return "Local Host Terminated";
    case 0x22: return "Failed to Establish";
    case 0x3E: return "LL Timeout";
    default:   return "other HCI";
    }
}

static int ble_config_gap_event(struct ble_gap_event *event, void *arg)
{
    struct ble_gap_conn_desc desc;
    int rc;

    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) {
            rc = ble_gap_conn_find(event->connect.conn_handle, &desc);
            if (rc == 0) {
                ESP_LOGI(TAG, "client connected: conn_handle=%d addr=%02X:%02X:%02X:%02X:%02X:%02X type=%d",
                         event->connect.conn_handle,
                         desc.peer_id_addr.val[5], desc.peer_id_addr.val[4],
                         desc.peer_id_addr.val[3], desc.peer_id_addr.val[2],
                         desc.peer_id_addr.val[1], desc.peer_id_addr.val[0],
                         desc.peer_id_addr.type);
            } else {
                ESP_LOGI(TAG, "client connected: conn_handle=%d", event->connect.conn_handle);
            }
            s_adv_active = false;
        } else {
            ESP_LOGW(TAG, "client connect failed: status=%d", event->connect.status);
            ble_config_advertise();
        }
        break;

    case BLE_GAP_EVENT_DISCONNECT: {
        int raw = event->disconnect.reason;
        ESP_LOGI(TAG, "client disconnected: conn_handle=%d reason=%d (0x%03X) [%s]",
                 event->disconnect.conn.conn_handle,
                 raw, (unsigned)raw,
                 ble_disconnect_reason_str(raw));
        ble_config_advertise();
        break;
    }

    case BLE_GAP_EVENT_ADV_COMPLETE:
        ESP_LOGI(TAG, "advertising complete, restarting");
        s_adv_active = false;
        ble_config_advertise();
        break;

    case BLE_GAP_EVENT_MTU:
        ESP_LOGI(TAG, "mtu updated: conn_handle=%d mtu=%d",
                 event->mtu.conn_handle, event->mtu.value);
        break;

    case BLE_GAP_EVENT_REPEAT_PAIRING:
        /* Windows may try to re-pair using a cached LTK that the ESP32 no longer has.
         * Delete the stale bond record and allow the pairing to proceed. */
        ESP_LOGW(TAG, "repeat pairing detected: deleting old bond and retrying");
        rc = ble_gap_conn_find(event->repeat_pairing.conn_handle, &desc);
        if (rc == 0) {
            ble_store_util_delete_peer(&desc.peer_id_addr);
        }
        return BLE_GAP_REPEAT_PAIRING_RETRY;

    case BLE_GAP_EVENT_PASSKEY_ACTION:
        ESP_LOGI(TAG, "PASSKEY_ACTION ignored: pairing disabled for test "
                      "(action=%d conn_handle=%d)",
                 event->passkey.params.action, event->passkey.conn_handle);
        return 0;

    case BLE_GAP_EVENT_ENC_CHANGE:
        ESP_LOGI(TAG, "encryption changed: conn_handle=%d status=%d",
                 event->enc_change.conn_handle, event->enc_change.status);
        break;

    case BLE_GAP_EVENT_SUBSCRIBE:
        ESP_LOGI(TAG, "subscribe: conn_handle=%d attr_handle=%d "
                      "reason=%d prev_notify=%d cur_notify=%d",
                 event->subscribe.conn_handle, event->subscribe.attr_handle,
                 event->subscribe.reason,
                 event->subscribe.prev_notify, event->subscribe.cur_notify);
        break;

    default:
        break;
    }

    return 0;
}

static void ble_config_on_sync(void)
{
    int rc;

    ESP_LOGI(TAG, "host synced");

    /* Clear any stale bond/LTK data from previous sessions.
     * Windows caches the LTK and tries to re-use it on reconnect;
     * if ESP32 doesn't have a matching record it rejects pairing → disconnect.
     * Clearing on every boot ensures a clean state. */
    ble_store_clear();

    rc = ble_hs_util_ensure_addr(0);
    if (rc != 0) {
        ESP_LOGE(TAG, "ensure address failed: %d", rc);
    }

    rc = ble_hs_id_infer_auto(0, &s_own_addr_type);
    if (rc != 0) {
        ESP_LOGE(TAG, "address type infer failed: %d", rc);
        return;
    }

    ble_config_log_addr();
    ESP_LOGI(TAG, "GATT handles: read=%u write=%u status=%u",
             s_read_handle, s_write_handle, s_status_handle);
    ble_config_advertise();
}

static void ble_config_on_reset(int reason)
{
    ESP_LOGE(TAG, "host reset; reason=%d", reason);
    s_adv_active = false;
}

static void ble_config_host_task(void *param)
{
    ESP_LOGI(TAG, "host task running");
    nimble_port_run();
    nimble_port_freertos_deinit();
}

static int ble_config_gatt_init(void)
{
    int rc;

    ble_svc_gap_init();
    ble_svc_gatt_init();

    rc = ble_gatts_count_cfg(gatt_svcs);
    if (rc != 0) {
        return rc;
    }

    rc = ble_gatts_add_svcs(gatt_svcs);
    if (rc != 0) {
        return rc;
    }

    return 0;
}

esp_err_t ble_config_start(void)
{
    esp_err_t ret;
    int rc;

    ESP_LOGI(TAG, "init start");

    ret = nimble_port_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "nimble_port_init failed: %s", esp_err_to_name(ret));
        return ret;
    }

    ble_hs_cfg.sync_cb = ble_config_on_sync;
    ble_hs_cfg.reset_cb = ble_config_on_reset;
    ble_hs_cfg.store_status_cb = ble_store_util_status_rr;

    /* Security Manager: open connection, no pairing/bonding/encryption required.
     * Windows BT stack initiates pairing after MTU negotiation.
     * Without this config NimBLE may reject the SM request → Windows disconnects. */
    ble_hs_cfg.sm_io_cap          = BLE_SM_IO_CAP_NO_IO;
    ble_hs_cfg.sm_bonding         = 0;
    ble_hs_cfg.sm_mitm            = 0;
    ble_hs_cfg.sm_sc              = 0;
    ble_hs_cfg.sm_our_key_dist    = 0;
    ble_hs_cfg.sm_their_key_dist  = 0;

    ble_store_config_init();

    rc = ble_config_gatt_init();
    if (rc != 0) {
        ESP_LOGE(TAG, "gatt init failed: %d", rc);
        return ESP_FAIL;
    }

    rc = ble_svc_gap_device_name_set(DEVICE_NAME);
    if (rc != 0) {
        ESP_LOGE(TAG, "device name set failed: %d", rc);
        return ESP_FAIL;
    }

    alarm_config_set_status("OK");
    nimble_port_freertos_init(ble_config_host_task);

    ESP_LOGI(TAG, "device name: %s", DEVICE_NAME);
    ESP_LOGI(TAG, "GATT server ready, waiting for host sync");
    return ESP_OK;
}
