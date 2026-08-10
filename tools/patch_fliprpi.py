from pathlib import Path

p = Path("FlipRPI/app.c")
s = p.read_text()


def one(old: str, new: str, label: str) -> None:
    global s
    count = s.count(old)
    if count != 1:
        raise SystemExit(f"patch {label}: expected 1 match, got {count}")
    s = s.replace(old, new, 1)


one(
    "#include <easy_flipper/easy_flipper.h>\n",
    "#include <easy_flipper/easy_flipper.h>\n"
    "#include <ble_serial/ble_serial.h>\n"
    "#include <bt/bt_service/bt.h>\n"
    "#include <furi_hal_bt.h>\n"
    "#include <storage/storage.h>\n",
    "includes",
)

one(
    '#define VERSION_TAG TAG " v1.0"',
    '#define VERSION_TAG TAG " v1.1 BLE"',
    "version tag",
)

one(
    "#define MAX_COMMANDS 54\n",
    "#define MAX_COMMANDS 54\n"
    "#define BLE_RX_PACKET_MAX 512\n"
    "#define BLE_COMMAND_MAX 512\n"
    "#define BLE_SERIAL_BUFFER_SIZE 4096\n\n"
    "typedef struct {\n"
    "    uint16_t size;\n"
    "    uint8_t data[BLE_RX_PACKET_MAX];\n"
    "} FlipRPIBlePacket;\n",
    "BLE constants",
)

one(
    "typedef enum\n{\n    FlipRPICustomEventUART\n} FlipRPICustomEvent;",
    "typedef enum\n{\n    FlipRPICustomEventUART,\n    FlipRPICustomEventBLE\n} FlipRPICustomEvent;",
    "custom event enum",
)

one(
    "    FuriTimer *timer; // timer to redraw the UART data as it comes in\n} FlipRPIApp;",
    "    FuriTimer *timer; // timer to redraw the UART data as it comes in\n\n"
    "    // BLE phone input: iPhone -> BLE -> FlipRPI -> Pi UART\n"
    "    Bt *bt;\n"
    "    FuriHalBleProfileBase *ble_serial_profile;\n"
    "    FuriMessageQueue *ble_rx_queue;\n"
    "    char ble_command[BLE_COMMAND_MAX];\n"
    "    size_t ble_command_len;\n"
    "} FlipRPIApp;",
    "app fields",
)

marker = "static void text_updated(void *context)\n"
if s.count(marker) != 1:
    raise SystemExit("text_updated marker mismatch")

ble_code = r'''
static bool flip_rpi_ensure_uart(FlipRPIApp *app)
{
    if (app->fhttp)
    {
        return true;
    }

    app->fhttp = flipper_http_alloc();
    if (!app->fhttp)
    {
        FURI_LOG_E(TAG, "Failed to allocate FlipperHTTP for BLE command");
        easy_flipper_dialog("Error", "Failed to start UART.\nUART may be busy.");
        return false;
    }
    return true;
}

static void flip_rpi_send_ble_command(FlipRPIApp *app)
{
    if (app->ble_command_len == 0)
    {
        return;
    }

    if (!flip_rpi_ensure_uart(app))
    {
        app->ble_command_len = 0;
        return;
    }

    char command[BLE_COMMAND_MAX + 2];
    size_t len = app->ble_command_len;
    memcpy(command, app->ble_command, len);
    command[len++] = '\n';
    command[len] = '\0';
    app->ble_command_len = 0;

    FURI_LOG_I(TAG, "BLE -> UART command (%zu bytes)", len - 1);
    if (!flipper_http_send_data(app->fhttp, command))
    {
        FURI_LOG_E(TAG, "Failed to send BLE command over UART");
        easy_flipper_dialog("Error", "BLE command failed to send over UART.");
        return;
    }

    if (app->textbox)
    {
        update_text_box(app);
    }
}

static void flip_rpi_process_ble_rx(FlipRPIApp *app)
{
    if (!app->ble_rx_queue)
    {
        return;
    }

    FlipRPIBlePacket packet;
    while (furi_message_queue_get(app->ble_rx_queue, &packet, 0) == FuriStatusOk)
    {
        for (uint16_t i = 0; i < packet.size; i++)
        {
            const char c = (char)packet.data[i];

            if (c == '\r' || c == '\n')
            {
                flip_rpi_send_ble_command(app);
                continue;
            }

            if (app->ble_command_len < BLE_COMMAND_MAX - 1)
            {
                app->ble_command[app->ble_command_len++] = c;
            }
            else
            {
                FURI_LOG_W(TAG, "BLE command too long; dropping buffered command");
                app->ble_command_len = 0;
            }
        }
    }
}

static uint16_t flip_rpi_ble_serial_callback(SerialServiceEvent event, void *context)
{
    FlipRPIApp *app = (FlipRPIApp *)context;
    if (!app || !app->ble_rx_queue)
    {
        return 0;
    }

    if (event.event == SerialServiceEventTypeDataReceived)
    {
        FlipRPIBlePacket packet = {0};
        packet.size = event.data.size > BLE_RX_PACKET_MAX ? BLE_RX_PACKET_MAX : event.data.size;
        memcpy(packet.data, event.data.buffer, packet.size);

        if (furi_message_queue_put(app->ble_rx_queue, &packet, 0) == FuriStatusOk)
        {
            view_dispatcher_send_custom_event(app->view_dispatcher, FlipRPICustomEventBLE);
        }
        else
        {
            FURI_LOG_W(TAG, "BLE RX queue full; packet dropped");
        }
    }

    return BLE_SERIAL_BUFFER_SIZE;
}

static bool flip_rpi_ble_start(FlipRPIApp *app)
{
    app->bt = furi_record_open(RECORD_BT);
    if (!app->bt)
    {
        return false;
    }

    bt_disconnect(app->bt);
    furi_delay_ms(200);

    bt_keys_storage_set_storage_path(app->bt, APP_DATA_PATH(".fliprpi_ble.keys"));

    BleProfileSerialParams params = {
        .device_name_prefix = "FlipRPI",
        .mac_xor = 0x0042,
    };

    app->ble_serial_profile = bt_profile_start(app->bt, ble_profile_serial, &params);
    if (!app->ble_serial_profile)
    {
        bt_keys_storage_set_default_path(app->bt);
        furi_record_close(RECORD_BT);
        app->bt = NULL;
        return false;
    }

    ble_profile_serial_set_event_callback(
        app->ble_serial_profile,
        BLE_SERIAL_BUFFER_SIZE,
        flip_rpi_ble_serial_callback,
        app);
    furi_hal_bt_start_advertising();
    FURI_LOG_I(TAG, "BLE phone input advertising as FlipRPI");
    return true;
}

static void flip_rpi_ble_stop(FlipRPIApp *app)
{
    if (!app->bt)
    {
        return;
    }

    if (app->ble_serial_profile)
    {
        ble_profile_serial_set_event_callback(app->ble_serial_profile, 0, NULL, NULL);
    }

    bt_disconnect(app->bt);
    furi_delay_ms(200);
    bt_keys_storage_set_default_path(app->bt);

    if (app->ble_serial_profile)
    {
        if (!bt_profile_restore_default(app->bt))
        {
            FURI_LOG_W(TAG, "Failed to restore default BLE profile");
        }
        app->ble_serial_profile = NULL;
    }

    furi_record_close(RECORD_BT);
    app->bt = NULL;
}

'''

s = s.replace(marker, ble_code + marker, 1)

one(
    "    case FlipRPICustomEventUART:\n        flip_rpi_loader_process_callback(context);\n        return true;\n",
    "    case FlipRPICustomEventUART:\n        flip_rpi_loader_process_callback(context);\n        return true;\n"
    "    case FlipRPICustomEventBLE:\n        flip_rpi_process_ble_rx((FlipRPIApp *)context);\n        return true;\n",
    "BLE event handler",
)

one(
    "    FlipRPIApp *app = (FlipRPIApp *)malloc(sizeof(FlipRPIApp));\n\n    Gui *gui",
    "    FlipRPIApp *app = (FlipRPIApp *)malloc(sizeof(FlipRPIApp));\n"
    "    if (!app)\n"
    "    {\n"
    "        return NULL;\n"
    "    }\n"
    "    memset(app, 0, sizeof(FlipRPIApp));\n\n"
    "    Gui *gui",
    "zero initialize app",
)

one(
    '    submenu_add_item(app->submenu, "About", FlipRPISubmenuIndexAbout, callback_submenu_choices, app);\n    // submenu_add_item',
    '    submenu_add_item(app->submenu, "About", FlipRPISubmenuIndexAbout, callback_submenu_choices, app);\n\n'
    "    app->ble_rx_queue = furi_message_queue_alloc(8, sizeof(FlipRPIBlePacket));\n"
    "    if (!app->ble_rx_queue)\n"
    "    {\n"
    '        FURI_LOG_E(TAG, "Failed to allocate BLE RX queue");\n'
    "        return NULL;\n"
    "    }\n"
    "    if (!flip_rpi_ble_start(app))\n"
    "    {\n"
    '        FURI_LOG_E(TAG, "BLE phone input failed to start; normal FlipRPI remains available");\n'
    "    }\n\n"
    "    // submenu_add_item",
    "start BLE",
)

one(
    "    free_widget(app);\n    free_text_input(app);\n    free_submenu_command(app);\n    free_text_box(app);\n\n    // free the FlipperHTTP",
    "    free_widget(app);\n    free_text_input(app);\n    free_submenu_command(app);\n    free_text_box(app);\n\n"
    "    flip_rpi_ble_stop(app);\n"
    "    if (app->ble_rx_queue)\n"
    "    {\n"
    "        furi_message_queue_free(app->ble_rx_queue);\n"
    "        app->ble_rx_queue = NULL;\n"
    "    }\n\n"
    "    // free the FlipperHTTP",
    "free BLE",
)

p.write_text(s)

fam = Path("FlipRPI/application.fam")
fs = fam.read_text()
fs = fs.replace("stack_size=4 * 1024", "stack_size=8 * 1024")
fs = fs.replace('fap_version="1.0"', 'fap_version="1.1"')
fam.write_text(fs)

print("FlipRPI patched for iPhone BLE serial command input")
