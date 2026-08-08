#include <furi.h>
#include <gui/gui.h>
#include <gui/elements.h>
#include <gui/view_dispatcher.h>
#include <gui/modules/text_input.h>
#include <input/input.h>
#include <bt/bt_service/bt.h>
#include <profiles/serial_profile.h>

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define TAG "PwnComp"
#define RX_LINE_MAX 496
#define RX_QUEUE_LEN 16
#define PLUGIN_MAX 64
#define PLUGIN_NAME_MAX 64
#define OPTION_MAX 64
#define OPTION_KEY_MAX 104
#define OPTION_VALUE_MAX 128
#define TOAST_MAX 48
#define EDIT_BUFFER_MAX 128
#define TX_PENDING_MAX 256
#define TX_BUSY_TIMEOUT_MS 1500

typedef enum {
    PageLive = 0,
    PageMenu,
    PagePlugins,
    PagePluginDetail,
    PageSystem,
    PageAbout,
} AppPage;

typedef struct {
    char name[PLUGIN_NAME_MAX];
    char version[24];
    bool enabled;
    bool found;
    uint8_t option_count;
    uint8_t missing_required;
} PluginItem;

typedef struct {
    char key[OPTION_KEY_MAX];
    char value[OPTION_VALUE_MAX];
    char type;
    bool required;
} OptionItem;

typedef struct PwnCompApp PwnCompApp;

struct PwnCompApp {
    Gui* gui;
    ViewPort* view_port;
    FuriMessageQueue* input_queue;
    FuriMutex* mutex;

    Bt* bt;
    FuriHalBleProfileBase* ble_profile;
    volatile bool connected;
    volatile bool need_bind;
    volatile bool tx_busy;
    uint32_t tx_busy_since;
    bool tx_pending;
    char pending_tx[TX_PENDING_MAX];

    char partial[RX_LINE_MAX];
    size_t partial_len;
    char rx_lines[RX_QUEUE_LEN][RX_LINE_MAX];
    uint8_t rx_head;
    uint8_t rx_tail;
    uint8_t rx_count;

    AppPage page;
    uint8_t menu_index;
    uint8_t plugin_index;
    uint8_t detail_index;
    uint8_t system_index;

    char name[24];
    char mode[8];
    char face[16];
    char channel[12];
    char aps[20];
    char shakes[20];
    char temp[12];
    char status[72];

    PluginItem plugins[PLUGIN_MAX];
    uint8_t plugin_count;
    bool plugin_loading;

    char detail_name[PLUGIN_NAME_MAX];
    char detail_version[24];
    char detail_description[96];
    char detail_source[112];
    bool detail_enabled;
    bool detail_found;
    uint8_t detail_missing_required;
    OptionItem options[OPTION_MAX];
    uint8_t option_count;
    bool detail_loading;

    bool restart_required;

    char toast[TOAST_MAX];
    uint32_t toast_until;
    int confirm_action;
    uint32_t confirm_until;

    ViewDispatcher* editor_dispatcher;
    TextInput* text_input;
    char edit_buffer[EDIT_BUFFER_MAX];
    bool edit_committed;
};

static void safe_copy(char* dst, size_t dst_size, const char* src) {
    if(!dst || dst_size == 0) return;
    if(!src) src = "";
    size_t i = 0;
    while(i + 1 < dst_size && src[i] != '\0') {
        dst[i] = src[i];
        i++;
    }
    dst[i] = '\0';
}

static void sanitize_text(char* s) {
    if(!s) return;
    for(char* p = s; *p; p++) {
        if(*p == '\r' || *p == '\n') *p = ' ';
    }
}

static int hex_value(char c) {
    if(c >= '0' && c <= '9') return c - '0';
    if(c >= 'A' && c <= 'F') return c - 'A' + 10;
    if(c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

static void wire_decode(char* dst, size_t dst_size, const char* src) {
    if(!dst || dst_size == 0) return;
    if(!src) src = "";
    size_t di = 0;
    for(size_t si = 0; src[si] && di + 1 < dst_size; si++) {
        if(src[si] == '%' && src[si + 1] && src[si + 2]) {
            int hi = hex_value(src[si + 1]);
            int lo = hex_value(src[si + 2]);
            if(hi >= 0 && lo >= 0) {
                dst[di++] = (char)((hi << 4) | lo);
                si += 2;
                continue;
            }
        }
        dst[di++] = src[si];
    }
    dst[di] = '\0';
}

static bool wire_char_safe(unsigned char c) {
    if((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')) return true;
    const char* safe = "-_.~ /:@[]{}(),+";
    return strchr(safe, (char)c) != NULL;
}

static bool wire_encode(char* dst, size_t dst_size, const char* src) {
    static const char hex[] = "0123456789ABCDEF";
    if(!dst || dst_size == 0) return false;
    if(!src) src = "";
    size_t di = 0;
    for(size_t si = 0; src[si]; si++) {
        unsigned char c = (unsigned char)src[si];
        if(wire_char_safe(c) && c != '%' && c != '|') {
            if(di + 1 >= dst_size) return false;
            dst[di++] = (char)c;
        } else {
            if(di + 3 >= dst_size) return false;
            dst[di++] = '%';
            dst[di++] = hex[(c >> 4) & 0x0F];
            dst[di++] = hex[c & 0x0F];
        }
    }
    dst[di] = '\0';
    return true;
}

static size_t split_fields(char* payload, char** fields, size_t max_fields) {
    size_t count = 0;
    char* cursor = payload;
    while(cursor && count < max_fields) {
        fields[count++] = cursor;
        char* pipe = strchr(cursor, '|');
        if(!pipe) break;
        *pipe = '\0';
        cursor = pipe + 1;
    }
    return count;
}

static void make_prefixed_line(char* dst, size_t dst_size, char prefix, const char* text) {
    if(!dst || dst_size == 0) return;
    size_t pos = 0;
    if(pos + 1 < dst_size) dst[pos++] = prefix;
    if(pos + 1 < dst_size) dst[pos++] = ' ';
    dst[pos] = '\0';
    if(pos < dst_size) safe_copy(dst + pos, dst_size - pos, text);
}

static void set_toast(PwnCompApp* app, const char* text) {
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    safe_copy(app->toast, sizeof(app->toast), text);
    app->toast_until = furi_get_tick() + furi_ms_to_ticks(2700);
    furi_mutex_release(app->mutex);
}

static bool toast_active(PwnCompApp* app) {
    return app->toast[0] && (int32_t)(app->toast_until - furi_get_tick()) > 0;
}

static void draw_header(Canvas* canvas, PwnCompApp* app, const char* title) {
    canvas_set_font(canvas, FontSecondary);
    canvas_draw_str(canvas, 2, 8, title);
    canvas_draw_str_aligned(canvas, 126, 8, AlignRight, AlignBottom, app->connected ? "BT" : "--");
    canvas_draw_line(canvas, 0, 10, 127, 10);
}

static void draw_live(Canvas* canvas, PwnCompApp* app) {
    char line[64];
    draw_header(canvas, app, app->name[0] ? app->name : "Pwnagotchi");
    canvas_set_font(canvas, FontPrimary);
    canvas_draw_str_aligned(canvas, 64, 25, AlignCenter, AlignCenter, app->face[0] ? app->face : "(-_-)");

    canvas_set_font(canvas, FontSecondary);
    snprintf(
        line,
        sizeof(line),
        "%s  CH:%s  %sC",
        app->mode[0] ? app->mode : "----",
        app->channel[0] ? app->channel : "-",
        app->temp[0] ? app->temp : "--");
    canvas_draw_str(canvas, 2, 36, line);

    snprintf(
        line,
        sizeof(line),
        "APS:%s  PWND:%s",
        app->aps[0] ? app->aps : "0",
        app->shakes[0] ? app->shakes : "0");
    canvas_draw_str(canvas, 2, 46, line);

    char status_short[38];
    safe_copy(status_short, sizeof(status_short), app->status);
    canvas_draw_str(canvas, 2, 57, status_short[0] ? status_short : "Waiting for Pi...");
    canvas_draw_str_aligned(canvas, 126, 63, AlignRight, AlignBottom, "OK:menu");
}

static const char* menu_items[] = {"Live", "Plugins", "System", "About"};
#define MENU_COUNT ((uint8_t)(sizeof(menu_items) / sizeof(menu_items[0])))

static void draw_menu(Canvas* canvas, PwnCompApp* app) {
    draw_header(canvas, app, "Pwnagotchi Menu");
    canvas_set_font(canvas, FontSecondary);
    for(uint8_t row = 0; row < MENU_COUNT; row++) {
        char line[28];
        make_prefixed_line(line, sizeof(line), row == app->menu_index ? '>' : ' ', menu_items[row]);
        canvas_draw_str(canvas, 4, 22 + row * 11, line);
    }
    if(app->restart_required) canvas_draw_str_aligned(canvas, 126, 63, AlignRight, AlignBottom, "restart *");
}

static void draw_plugins(Canvas* canvas, PwnCompApp* app) {
    draw_header(canvas, app, "Plugins  OK=open");
    canvas_set_font(canvas, FontSecondary);
    if(!app->connected) {
        canvas_draw_str(canvas, 5, 29, "Pi not connected");
        return;
    }
    if(app->plugin_loading) {
        canvas_draw_str(canvas, 5, 25, "Scanning Pi plugins...");
        if(app->plugin_count == 0) canvas_draw_str(canvas, 5, 39, "Reading files + config");
    }
    if(app->plugin_count == 0 && !app->plugin_loading) {
        canvas_draw_str(canvas, 5, 28, "No plugins reported");
        canvas_draw_str(canvas, 5, 42, "RIGHT = rescan");
        return;
    }

    uint8_t start = app->plugin_index > 2 ? app->plugin_index - 2 : 0;
    if(start + 5 > app->plugin_count) start = app->plugin_count > 5 ? app->plugin_count - 5 : 0;
    for(uint8_t row = 0; row < 5 && start + row < app->plugin_count; row++) {
        uint8_t idx = start + row;
        char line[48];
        snprintf(
            line,
            sizeof(line),
            "%c[%c]%c %.27s%s",
            idx == app->plugin_index ? '>' : ' ',
            app->plugins[idx].enabled ? 'x' : ' ',
            app->plugins[idx].found ? ' ' : '?',
            app->plugins[idx].name,
            app->plugins[idx].missing_required ? " !" : "");
        canvas_draw_str(canvas, 1, 20 + row * 10, line);
    }
    canvas_draw_str_aligned(canvas, 126, 63, AlignRight, AlignBottom, "R:scan");
}

static const char* option_value_display(const OptionItem* option, char* out, size_t out_size) {
    if(option->type == 'b') {
        safe_copy(out, out_size, (strcmp(option->value, "true") == 0 || strcmp(option->value, "1") == 0) ? "ON" : "OFF");
    } else if(option->value[0]) {
        safe_copy(out, out_size, option->value);
    } else {
        safe_copy(out, out_size, option->required ? "<REQUIRED>" : "<empty>");
    }
    return out;
}

static void draw_plugin_detail(Canvas* canvas, PwnCompApp* app) {
    char title[64];
    snprintf(title, sizeof(title), "%.38s%s", app->detail_name[0] ? app->detail_name : "Plugin", app->detail_found ? "" : " ?");
    draw_header(canvas, app, title);
    canvas_set_font(canvas, FontSecondary);

    if(app->detail_loading) {
        canvas_draw_str(canvas, 4, 20, "Loading settings...");
        return;
    }

    char meta[48];
    snprintf(
        meta,
        sizeof(meta),
        "v%.12s  opts:%u  missing:%u",
        app->detail_version[0] ? app->detail_version : "?",
        app->option_count,
        app->detail_missing_required);
    canvas_draw_str(canvas, 3, 19, meta);

    uint8_t total = (uint8_t)(app->option_count + 1);
    uint8_t start = app->detail_index > 1 ? app->detail_index - 1 : 0;
    if(start + 4 > total) start = total > 4 ? total - 4 : 0;

    for(uint8_t row = 0; row < 4 && start + row < total; row++) {
        uint8_t idx = start + row;
        char body[52];
        if(idx == 0) {
            snprintf(body, sizeof(body), "Enabled: %s", app->detail_enabled ? "ON" : "OFF");
        } else {
            OptionItem* option = &app->options[idx - 1];
            char value[28];
            option_value_display(option, value, sizeof(value));
            snprintf(
                body,
                sizeof(body),
                "%s%.20s: %.18s",
                option->required ? "!" : "",
                option->key,
                value);
        }
        char line[56];
        make_prefixed_line(line, sizeof(line), idx == app->detail_index ? '>' : ' ', body);
        canvas_draw_str(canvas, 2, 29 + row * 9, line);
    }
    canvas_draw_str_aligned(canvas, 126, 63, AlignRight, AlignBottom, "OK:edit L/R:quick");
}

static const char* system_items[] = {"Refresh state", "Rescan plugins", "Apply + restart", "Reboot Pi"};
#define SYSTEM_COUNT ((uint8_t)(sizeof(system_items) / sizeof(system_items[0])))

static void draw_system(Canvas* canvas, PwnCompApp* app) {
    draw_header(canvas, app, "System");
    canvas_set_font(canvas, FontSecondary);
    for(uint8_t i = 0; i < SYSTEM_COUNT; i++) {
        char body[40];
        safe_copy(body, sizeof(body), system_items[i]);
        if(i == 2 && app->restart_required) {
            size_t len = strlen(body);
            if(len + 2 < sizeof(body)) {
                body[len++] = ' ';
                body[len++] = '*';
                body[len] = '\0';
            }
        }
        char line[44];
        make_prefixed_line(line, sizeof(line), i == app->system_index ? '>' : ' ', body);
        canvas_draw_str(canvas, 4, 21 + i * 11, line);
    }
    if(app->confirm_action >= 0 && (int32_t)(app->confirm_until - furi_get_tick()) > 0) {
        canvas_draw_str(canvas, 3, 63, "OK again to confirm");
    }
}

static void draw_about(Canvas* canvas, PwnCompApp* app) {
    draw_header(canvas, app, "Pwnagotchi Companion");
    canvas_set_font(canvas, FontSecondary);
    canvas_draw_str(canvas, 4, 24, "v0.2.1 dynamic config");
    canvas_draw_str(canvas, 4, 36, "Plugins scanned by Pi");
    canvas_draw_str(canvas, 4, 48, "Typed settings over BLE");
    canvas_draw_str(canvas, 4, 60, "No plugin hard-coding");
}

static void draw_callback(Canvas* canvas, void* context) {
    PwnCompApp* app = context;
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    canvas_clear(canvas);
    switch(app->page) {
    case PageLive:
        draw_live(canvas, app);
        break;
    case PageMenu:
        draw_menu(canvas, app);
        break;
    case PagePlugins:
        draw_plugins(canvas, app);
        break;
    case PagePluginDetail:
        draw_plugin_detail(canvas, app);
        break;
    case PageSystem:
        draw_system(canvas, app);
        break;
    case PageAbout:
        draw_about(canvas, app);
        break;
    }
    if(toast_active(app)) {
        canvas_set_color(canvas, ColorWhite);
        canvas_draw_box(canvas, 0, 51, 128, 13);
        canvas_set_color(canvas, ColorBlack);
        canvas_draw_frame(canvas, 0, 51, 128, 13);
        canvas_set_font(canvas, FontSecondary);
        canvas_draw_str(canvas, 3, 61, app->toast);
    }
    furi_mutex_release(app->mutex);
}

static void input_callback(InputEvent* event, void* context) {
    PwnCompApp* app = context;
    furi_message_queue_put(app->input_queue, event, 0);
}

static uint16_t serial_event_callback(SerialServiceEvent event, void* context) {
    PwnCompApp* app = context;
    if(event.event == SerialServiceEventTypeDataSent) {
        app->tx_busy = false;
        app->tx_busy_since = 0;
        return RX_LINE_MAX * RX_QUEUE_LEN;
    }
    if(event.event == SerialServiceEventTypesBleResetRequest) {
        app->connected = false;
        app->need_bind = true;
        return RX_LINE_MAX * RX_QUEUE_LEN;
    }
    if(event.event != SerialServiceEventTypeDataReceived) return RX_LINE_MAX * RX_QUEUE_LEN;

    furi_mutex_acquire(app->mutex, FuriWaitForever);
    for(uint16_t i = 0; i < event.data.size; i++) {
        char c = (char)event.data.buffer[i];
        if(c == '\r') continue;
        if(c == '\n') {
            if(app->partial_len > 0) {
                app->partial[app->partial_len] = '\0';
                if(app->rx_count < RX_QUEUE_LEN) {
                    safe_copy(app->rx_lines[app->rx_tail], RX_LINE_MAX, app->partial);
                    app->rx_tail = (app->rx_tail + 1) % RX_QUEUE_LEN;
                    app->rx_count++;
                }
                app->partial_len = 0;
            }
        } else if(app->partial_len + 1 < sizeof(app->partial)) {
            app->partial[app->partial_len++] = c;
        }
    }
    furi_mutex_release(app->mutex);
    return RX_LINE_MAX * RX_QUEUE_LEN;
}

static void bt_status_callback(BtStatus status, void* context) {
    PwnCompApp* app = context;
    app->connected = status == BtStatusConnected;
    if(app->connected) app->need_bind = true;
}

static bool tx_busy_expired(PwnCompApp* app) {
    if(!app->tx_busy || app->tx_busy_since == 0) return false;
    return (int32_t)(furi_get_tick() - app->tx_busy_since) >=
           (int32_t)furi_ms_to_ticks(TX_BUSY_TIMEOUT_MS);
}

static bool send_packet(PwnCompApp* app, const char* text) {
    if(!app->connected || !app->ble_profile || !text) return false;

    /* A missed indication confirmation must not wedge outgoing commands forever. */
    if(app->tx_busy && tx_busy_expired(app)) {
        FURI_LOG_W(TAG, "TX busy timeout, recovering");
        app->tx_busy = false;
        app->tx_busy_since = 0;
    }
    if(app->tx_busy) return false;

    size_t len = strlen(text);
    if(len == 0 || len > BLE_PROFILE_SERIAL_PACKET_SIZE_MAX) return false;
    app->tx_busy = true;
    app->tx_busy_since = furi_get_tick();
    bool ok = ble_profile_serial_tx(app->ble_profile, (uint8_t*)text, (uint16_t)len);
    if(!ok) {
        app->tx_busy = false;
        app->tx_busy_since = 0;
    }
    return ok;
}

static bool queue_packet(PwnCompApp* app, const char* text) {
    if(!app->connected || !app->ble_profile || !text) return false;
    size_t len = strlen(text);
    if(len == 0 || len >= sizeof(app->pending_tx) || len > BLE_PROFILE_SERIAL_PACKET_SIZE_MAX) {
        return false;
    }
    if(send_packet(app, text)) return true;
    safe_copy(app->pending_tx, sizeof(app->pending_tx), text);
    app->tx_pending = true;
    return true;
}

static void service_pending_tx(PwnCompApp* app) {
    if(!app->tx_pending || !app->connected || !app->ble_profile) return;
    if(app->tx_busy && !tx_busy_expired(app)) return;
    if(app->tx_busy) {
        FURI_LOG_W(TAG, "TX confirmation timeout; retrying queued command");
        app->tx_busy = false;
        app->tx_busy_since = 0;
    }

    char packet[TX_PENDING_MAX];
    safe_copy(packet, sizeof(packet), app->pending_tx);
    if(send_packet(app, packet)) {
        app->tx_pending = false;
        app->pending_tx[0] = '\0';
    }
}

static int plugin_find(PwnCompApp* app, const char* name) {
    for(uint8_t i = 0; i < app->plugin_count; i++) {
        if(strcmp(app->plugins[i].name, name) == 0) return i;
    }
    return -1;
}

static void parse_state(PwnCompApp* app, char* payload) {
    char* cursor = payload;
    while(cursor && *cursor) {
        char* next = strchr(cursor, '|');
        if(next) *next = '\0';
        char* eq = strchr(cursor, '=');
        if(eq) {
            *eq = '\0';
            const char* key = cursor;
            const char* value = eq + 1;
            if(strcmp(key, "name") == 0) safe_copy(app->name, sizeof(app->name), value);
            else if(strcmp(key, "mode") == 0) safe_copy(app->mode, sizeof(app->mode), value);
            else if(strcmp(key, "face") == 0) safe_copy(app->face, sizeof(app->face), value);
            else if(strcmp(key, "ch") == 0) safe_copy(app->channel, sizeof(app->channel), value);
            else if(strcmp(key, "aps") == 0) safe_copy(app->aps, sizeof(app->aps), value);
            else if(strcmp(key, "shakes") == 0) safe_copy(app->shakes, sizeof(app->shakes), value);
            else if(strcmp(key, "temp") == 0) safe_copy(app->temp, sizeof(app->temp), value);
            else if(strcmp(key, "status") == 0) safe_copy(app->status, sizeof(app->status), value);
        }
        cursor = next ? next + 1 : NULL;
    }
}

static void parse_plugin_item(PwnCompApp* app, char* payload) {
    char* fields[6];
    if(split_fields(payload, fields, 6) < 6) return;
    char name[PLUGIN_NAME_MAX];
    char version[24];
    wire_decode(name, sizeof(name), fields[0]);
    wire_decode(version, sizeof(version), fields[5]);
    int idx = plugin_find(app, name);
    if(idx < 0 && app->plugin_count < PLUGIN_MAX) idx = app->plugin_count++;
    if(idx < 0) return;
    safe_copy(app->plugins[idx].name, sizeof(app->plugins[idx].name), name);
    safe_copy(app->plugins[idx].version, sizeof(app->plugins[idx].version), version);
    app->plugins[idx].enabled = atoi(fields[1]) != 0;
    app->plugins[idx].found = atoi(fields[2]) != 0;
    app->plugins[idx].option_count = (uint8_t)atoi(fields[3]);
    app->plugins[idx].missing_required = (uint8_t)atoi(fields[4]);
}

static void parse_detail_begin(PwnCompApp* app, char* payload) {
    char* fields[8];
    if(split_fields(payload, fields, 8) < 8) return;
    wire_decode(app->detail_name, sizeof(app->detail_name), fields[0]);
    app->detail_enabled = atoi(fields[1]) != 0;
    app->detail_found = atoi(fields[2]) != 0;
    app->detail_missing_required = (uint8_t)atoi(fields[4]);
    wire_decode(app->detail_version, sizeof(app->detail_version), fields[5]);
    wire_decode(app->detail_description, sizeof(app->detail_description), fields[6]);
    wire_decode(app->detail_source, sizeof(app->detail_source), fields[7]);
    app->option_count = 0;
    app->detail_index = 0;
    app->detail_loading = true;
}

static void parse_option(PwnCompApp* app, char* payload) {
    char* fields[4];
    if(split_fields(payload, fields, 4) < 4 || app->option_count >= OPTION_MAX) return;
    OptionItem* option = &app->options[app->option_count++];
    option->type = fields[0][0] ? fields[0][0] : 's';
    option->required = atoi(fields[1]) != 0;
    wire_decode(option->key, sizeof(option->key), fields[2]);
    wire_decode(option->value, sizeof(option->value), fields[3]);
}

static void parse_line(PwnCompApp* app, char* line) {
    sanitize_text(line);
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    if(strncmp(line, "S|", 2) == 0) {
        parse_state(app, line + 2);
    } else if(strncmp(line, "PB|", 3) == 0) {
        app->plugin_count = 0;
        app->plugin_index = 0;
        app->plugin_loading = true;
    } else if(strncmp(line, "P|", 2) == 0) {
        parse_plugin_item(app, line + 2);
    } else if(strncmp(line, "PE|", 3) == 0) {
        app->plugin_loading = false;
        if(app->plugin_count && app->plugin_index >= app->plugin_count) app->plugin_index = app->plugin_count - 1;
    } else if(strncmp(line, "OB|", 3) == 0) {
        parse_detail_begin(app, line + 3);
    } else if(strncmp(line, "O|", 2) == 0) {
        parse_option(app, line + 2);
    } else if(strncmp(line, "OE|", 3) == 0) {
        app->detail_loading = false;
        uint8_t total = (uint8_t)(app->option_count + 1);
        if(total && app->detail_index >= total) app->detail_index = total - 1;
    } else if(strncmp(line, "R|", 2) == 0) {
        app->restart_required = atoi(line + 2) != 0;
    } else if(strncmp(line, "A|", 2) == 0 || strncmp(line, "E|", 2) == 0) {
        char decoded[TOAST_MAX];
        wire_decode(decoded, sizeof(decoded), line + 2);
        safe_copy(app->toast, sizeof(app->toast), decoded);
        app->toast_until = furi_get_tick() + furi_ms_to_ticks(strncmp(line, "E|", 2) == 0 ? 3800 : 2700);
    }
    furi_mutex_release(app->mutex);
}

static bool pop_line(PwnCompApp* app, char* out) {
    bool ok = false;
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    if(app->rx_count) {
        safe_copy(out, RX_LINE_MAX, app->rx_lines[app->rx_head]);
        app->rx_head = (app->rx_head + 1) % RX_QUEUE_LEN;
        app->rx_count--;
        ok = true;
    }
    furi_mutex_release(app->mutex);
    return ok;
}

static bool send_get_plugin(PwnCompApp* app, const char* plugin_name) {
    char encoded[PLUGIN_NAME_MAX * 3];
    char packet[256];
    if(!wire_encode(encoded, sizeof(encoded), plugin_name)) return false;
    int written = snprintf(packet, sizeof(packet), "GET|PLUGIN|%s\n", encoded);
    if(written <= 0 || (size_t)written >= sizeof(packet)) return false;
    return queue_packet(app, packet);
}

static bool send_toggle_plugin(PwnCompApp* app, const char* plugin_name) {
    char encoded[PLUGIN_NAME_MAX * 3];
    char packet[256];
    if(!wire_encode(encoded, sizeof(encoded), plugin_name)) return false;
    int written = snprintf(packet, sizeof(packet), "TOGGLE|PLUGIN|%s\n", encoded);
    if(written <= 0 || (size_t)written >= sizeof(packet)) return false;
    return queue_packet(app, packet);
}

static bool append_packet_text(char* dst, size_t dst_size, size_t* pos, const char* src) {
    if(!dst || !pos || !src || *pos >= dst_size) return false;
    while(*src) {
        if(*pos + 1 >= dst_size) return false;
        dst[(*pos)++] = *src++;
    }
    dst[*pos] = '\0';
    return true;
}

static bool send_set_option(PwnCompApp* app, OptionItem* option, const char* value) {
    char plugin_enc[PLUGIN_NAME_MAX * 3];
    char key_enc[OPTION_KEY_MAX * 3];
    char value_enc[OPTION_VALUE_MAX * 3];
    char packet[BLE_PROFILE_SERIAL_PACKET_SIZE_MAX + 1];
    if(!wire_encode(plugin_enc, sizeof(plugin_enc), app->detail_name)) return false;
    if(!wire_encode(key_enc, sizeof(key_enc), option->key)) return false;
    if(!wire_encode(value_enc, sizeof(value_enc), value)) return false;

    size_t pos = 0;
    packet[0] = '\0';
    if(!append_packet_text(packet, sizeof(packet), &pos, "SETOPT|")) return false;
    if(!append_packet_text(packet, sizeof(packet), &pos, plugin_enc)) return false;
    if(!append_packet_text(packet, sizeof(packet), &pos, "|")) return false;
    if(!append_packet_text(packet, sizeof(packet), &pos, key_enc)) return false;
    if(!append_packet_text(packet, sizeof(packet), &pos, "|")) return false;
    if(pos + 2 >= sizeof(packet)) return false;
    packet[pos++] = option->type;
    packet[pos++] = '|';
    packet[pos] = '\0';
    if(!append_packet_text(packet, sizeof(packet), &pos, value_enc)) return false;
    if(!append_packet_text(packet, sizeof(packet), &pos, "\n")) return false;
    return queue_packet(app, packet);
}

static void editor_done(void* context) {
    PwnCompApp* app = context;
    app->edit_committed = true;
    if(app->editor_dispatcher) view_dispatcher_stop(app->editor_dispatcher);
}

static bool editor_back(void* context) {
    PwnCompApp* app = context;
    app->edit_committed = false;
    if(app->editor_dispatcher) view_dispatcher_stop(app->editor_dispatcher);
    return true;
}

static bool run_text_editor(PwnCompApp* app, const char* header, const char* initial) {
    safe_copy(app->edit_buffer, sizeof(app->edit_buffer), initial);
    app->edit_committed = false;
    view_port_enabled_set(app->view_port, false);

    app->editor_dispatcher = view_dispatcher_alloc();
    app->text_input = text_input_alloc();
    text_input_set_header_text(app->text_input, header);
    text_input_set_result_callback(
        app->text_input,
        editor_done,
        app,
        app->edit_buffer,
        sizeof(app->edit_buffer),
        false);
    view_dispatcher_set_event_callback_context(app->editor_dispatcher, app);
    view_dispatcher_set_navigation_event_callback(app->editor_dispatcher, editor_back);
    view_dispatcher_add_view(app->editor_dispatcher, 0, text_input_get_view(app->text_input));
    view_dispatcher_attach_to_gui(app->editor_dispatcher, app->gui, ViewDispatcherTypeFullscreen);
    view_dispatcher_switch_to_view(app->editor_dispatcher, 0);
    view_dispatcher_run(app->editor_dispatcher);

    view_dispatcher_remove_view(app->editor_dispatcher, 0);
    text_input_free(app->text_input);
    app->text_input = NULL;
    view_dispatcher_free(app->editor_dispatcher);
    app->editor_dispatcher = NULL;
    view_port_enabled_set(app->view_port, true);
    return app->edit_committed;
}

static void edit_option_text(PwnCompApp* app, OptionItem* option) {
    char header[48];
    snprintf(header, sizeof(header), "Edit %.38s", option->key);
    if(run_text_editor(app, header, option->value)) {
        if(!send_set_option(app, option, app->edit_buffer)) set_toast(app, "Could not send value");
        else app->detail_loading = true;
    }
}

static void quick_change_option(PwnCompApp* app, OptionItem* option, int direction) {
    char value[OPTION_VALUE_MAX];
    if(option->type == 'b') {
        bool enabled = strcmp(option->value, "true") == 0 || strcmp(option->value, "1") == 0;
        safe_copy(value, sizeof(value), enabled ? "false" : "true");
    } else if(option->type == 'i') {
        long current = strtol(option->value[0] ? option->value : "0", NULL, 10);
        long magnitude = current < 0 ? -current : current;
        long step = magnitude >= 100 ? 10 : 1;
        snprintf(value, sizeof(value), "%ld", current + direction * step);
    } else if(option->type == 'f') {
        float current = strtof(option->value[0] ? option->value : "0", NULL);
        float abs_current = current < 0 ? -current : current;
        float step = abs_current < 1.0f ? 0.05f : 0.1f;
        snprintf(value, sizeof(value), "%.3g", (double)(current + direction * step));
    } else {
        return;
    }
    if(!send_set_option(app, option, value)) set_toast(app, "Send failed");
    else app->detail_loading = true;
}

static void request_plugin_scan(PwnCompApp* app) {
    if(!app->connected) {
        set_toast(app, "Pi not connected");
        return;
    }
    if(queue_packet(app, "GET|PLUGINS\n")) {
        app->plugin_loading = true;
        app->plugin_count = 0;
        app->plugin_index = 0;
        set_toast(app, "Scanning plugins...");
    } else {
        set_toast(app, "Could not queue scan");
    }
}

static void open_selected_menu(PwnCompApp* app) {
    switch(app->menu_index) {
    case 0:
        app->page = PageLive;
        break;
    case 1:
        app->page = PagePlugins;
        request_plugin_scan(app);
        break;
    case 2:
        app->page = PageSystem;
        app->confirm_action = -1;
        break;
    case 3:
        app->page = PageAbout;
        break;
    }
}

static void handle_input(PwnCompApp* app, InputEvent* event, bool* running) {
    if(event->type != InputTypeShort) return;

    if(app->page == PageLive) {
        if(event->key == InputKeyOk) app->page = PageMenu;
        else if(event->key == InputKeyBack) *running = false;
        return;
    }

    if(event->key == InputKeyBack) {
        if(app->page == PageMenu) app->page = PageLive;
        else if(app->page == PagePluginDetail) app->page = PagePlugins;
        else app->page = PageMenu;
        app->confirm_action = -1;
        return;
    }

    if(app->page == PageMenu) {
        if(event->key == InputKeyUp && app->menu_index > 0) app->menu_index--;
        else if(event->key == InputKeyDown && app->menu_index + 1 < MENU_COUNT) app->menu_index++;
        else if(event->key == InputKeyOk) open_selected_menu(app);
        return;
    }

    if(app->page == PagePlugins) {
        if(event->key == InputKeyUp && app->plugin_index > 0) app->plugin_index--;
        else if(event->key == InputKeyDown && app->plugin_index + 1 < app->plugin_count) app->plugin_index++;
        else if(event->key == InputKeyRight) request_plugin_scan(app);
        else if(event->key == InputKeyOk && app->plugin_count && !app->plugin_loading) {
            app->page = PagePluginDetail;
            app->detail_loading = true;
            app->detail_index = 0;
            if(!send_get_plugin(app, app->plugins[app->plugin_index].name)) {
                app->detail_loading = false;
                set_toast(app, "Send pending");
            }
        }
        return;
    }

    if(app->page == PagePluginDetail) {
        uint8_t total = (uint8_t)(app->option_count + 1);
        if(event->key == InputKeyUp && app->detail_index > 0) app->detail_index--;
        else if(event->key == InputKeyDown && app->detail_index + 1 < total) app->detail_index++;
        else if(app->detail_loading) return;
        else if(app->detail_index == 0 &&
                (event->key == InputKeyOk || event->key == InputKeyLeft || event->key == InputKeyRight)) {
            if(!send_toggle_plugin(app, app->detail_name)) set_toast(app, "Send failed");
            else app->detail_loading = true;
        } else if(app->detail_index > 0 && app->detail_index <= app->option_count) {
            OptionItem* option = &app->options[app->detail_index - 1];
            if(event->key == InputKeyLeft) quick_change_option(app, option, -1);
            else if(event->key == InputKeyRight) quick_change_option(app, option, 1);
            else if(event->key == InputKeyOk) {
                if(option->type == 'b') quick_change_option(app, option, 1);
                else edit_option_text(app, option);
            }
        }
        return;
    }

    if(app->page == PageSystem) {
        if(event->key == InputKeyUp && app->system_index > 0) app->system_index--;
        else if(event->key == InputKeyDown && app->system_index + 1 < SYSTEM_COUNT) app->system_index++;
        else if(event->key == InputKeyOk) {
            if(app->system_index == 0) {
                if(!queue_packet(app, "GET|ALL\n")) set_toast(app, "Send failed");
                else set_toast(app, "State refresh requested");
            } else if(app->system_index == 1) {
                request_plugin_scan(app);
                app->page = PagePlugins;
            } else {
                int action = app->system_index;
                bool confirmed = app->confirm_action == action &&
                                 (int32_t)(app->confirm_until - furi_get_tick()) > 0;
                if(!confirmed) {
                    app->confirm_action = action;
                    app->confirm_until = furi_get_tick() + furi_ms_to_ticks(5000);
                } else {
                    if(action == 2) queue_packet(app, "ACTION|restart\n");
                    else if(action == 3) queue_packet(app, "ACTION|reboot\n");
                    app->confirm_action = -1;
                }
            }
        }
    }
}

static PwnCompApp* app_alloc(void) {
    PwnCompApp* app = calloc(1, sizeof(PwnCompApp));
    app->mutex = furi_mutex_alloc(FuriMutexTypeNormal);
    app->input_queue = furi_message_queue_alloc(8, sizeof(InputEvent));
    app->gui = furi_record_open(RECORD_GUI);
    app->bt = furi_record_open(RECORD_BT);
    app->view_port = view_port_alloc();
    view_port_draw_callback_set(app->view_port, draw_callback, app);
    view_port_input_callback_set(app->view_port, input_callback, app);
    gui_add_view_port(app->gui, app->view_port, GuiLayerFullscreen);

    app->page = PageLive;
    app->confirm_action = -1;
    safe_copy(app->name, sizeof(app->name), "Pwnagotchi");
    safe_copy(app->face, sizeof(app->face), "(-_-)");
    safe_copy(app->mode, sizeof(app->mode), "----");
    return app;
}

static void app_free(PwnCompApp* app) {
    if(!app) return;
    bt_set_status_changed_callback(app->bt, NULL, NULL);
    if(app->ble_profile) {
        ble_profile_serial_set_event_callback(app->ble_profile, 0, NULL, NULL);
        bt_profile_restore_default(app->bt);
        app->ble_profile = NULL;
    }
    view_port_enabled_set(app->view_port, false);
    gui_remove_view_port(app->gui, app->view_port);
    view_port_free(app->view_port);
    furi_record_close(RECORD_BT);
    furi_record_close(RECORD_GUI);
    furi_message_queue_free(app->input_queue);
    furi_mutex_free(app->mutex);
    free(app);
}

int32_t pwnagotchi_companion_app(void* p) {
    UNUSED(p);
    PwnCompApp* app = app_alloc();

    bt_set_status_changed_callback(app->bt, bt_status_callback, app);
    app->ble_profile = bt_profile_start(app->bt, ble_profile_serial, NULL);
    if(!app->ble_profile) set_toast(app, "Could not start BLE");

    bool running = true;
    InputEvent event;
    while(running) {
        if(app->need_bind && app->connected && app->ble_profile) {
            app->need_bind = false;
            ble_profile_serial_set_event_callback(
                app->ble_profile,
                RX_LINE_MAX * RX_QUEUE_LEN,
                serial_event_callback,
                app);
            ble_profile_serial_set_rpc_active(app->ble_profile, false);
            app->tx_busy = false;
            app->tx_busy_since = 0;
            app->tx_pending = false;
            app->pending_tx[0] = '\0';
            furi_delay_ms(100);
            /* Deliberately do NOT scan plugins here. Plugins are discovered on demand. */
            queue_packet(app, "HELLO|2\nGET|ALL\n");
        }

        char line[RX_LINE_MAX];
        while(pop_line(app, line)) parse_line(app, line);
        if(app->ble_profile && app->connected) ble_profile_serial_notify_buffer_is_empty(app->ble_profile);
        service_pending_tx(app);

        FuriStatus status = furi_message_queue_get(app->input_queue, &event, 50);
        if(status == FuriStatusOk) handle_input(app, &event, &running);

        if(app->confirm_action >= 0 && (int32_t)(app->confirm_until - furi_get_tick()) <= 0) {
            app->confirm_action = -1;
        }
        view_port_update(app->view_port);
    }

    app_free(app);
    return 0;
}
