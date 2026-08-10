from pathlib import Path

p = Path("FlipRPI/app.c")
s = p.read_text()

def one(old, new, label):
    global s
    c=s.count(old)
    if c!=1: raise SystemExit(f"patch {label}: expected 1 match, got {c}")
    s=s.replace(old,new,1)

one("#include <easy_flipper/easy_flipper.h>\n", "#include <easy_flipper/easy_flipper.h>\n#include <ble_serial/ble_serial.h>\n#include <bt/bt_service/bt.h>\n#include <furi_hal_bt.h>\n#include <storage/storage.h>\n", "includes")
one('#define VERSION_TAG TAG " v1.0"', '#define VERSION_TAG TAG " v2.0 BLE"', "version")
one("#define MAX_COMMANDS 54\n", "#define MAX_COMMANDS 54\n#define BLE_RX_PACKET_MAX 512\n#define BLE_COMMAND_MAX 512\n#define BLE_SERIAL_BUFFER_SIZE 4096\n#define BLE_COMMAND_IDLE_MS 250\n#define SPECIAL_KEY_START 1000\n\ntypedef struct { uint16_t size; uint8_t data[BLE_RX_PACKET_MAX]; } FlipRPIBlePacket;\n", "constants")

one("    FlipRPISubmenuIndexSend, // Click to send a command to the RPI\n    FlipRPISubmenuIndexAbout,", "    FlipRPISubmenuIndexSend, // Click to send a command to the RPI\n    FlipRPISubmenuIndexSpecialKeys,\n    FlipRPISubmenuIndexAbout,", "menu enum")
one("    FlipRPIViewSubmenuCommands, // The submenu for commands\n} FlipRPIView;", "    FlipRPIViewSubmenuCommands, // The submenu for commands\n    FlipRPIViewSpecialKeys,\n} FlipRPIView;", "view enum")
one("typedef enum\n{\n    FlipRPICustomEventUART\n} FlipRPICustomEvent;", "typedef enum\n{\n    FlipRPICustomEventUART, FlipRPICustomEventBLE, FlipRPICustomEventBLEFlush\n} FlipRPICustomEvent;", "events")
one("    Submenu *submenu_commands;            // The submenu for commands\n", "    Submenu *submenu_commands;            // The submenu for commands\n    Submenu *submenu_special;             // terminal control keys\n", "special submenu field")
one("    FuriTimer *timer; // timer to redraw the UART data as it comes in\n} FlipRPIApp;", "    FuriTimer *timer; // timer to redraw the UART data as it comes in\n    Bt *bt;\n    FuriHalBleProfileBase *ble_serial_profile;\n    FuriMessageQueue *ble_rx_queue;\n    FuriTimer *ble_send_timer;\n    char ble_command[BLE_COMMAND_MAX];\n    size_t ble_command_len;\n} FlipRPIApp;", "fields")

marker="static void text_updated(void *context)\n"
if s.count(marker)!=1: raise SystemExit("text marker")
ble=r'''
static bool flip_rpi_ensure_uart(FlipRPIApp *app) {
    if(app->fhttp) return true;
    app->fhttp=flipper_http_alloc();
    if(!app->fhttp){ easy_flipper_dialog("Error","Failed to start UART."); return false; }
    return true;
}

static bool flip_rpi_send_raw(FlipRPIApp *app, const uint8_t *data, size_t len) {
    if(!len || !flip_rpi_ensure_uart(app)) return false;
    char out[BLE_COMMAND_MAX+2];
    if(len>BLE_COMMAND_MAX) len=BLE_COMMAND_MAX;
    memcpy(out,data,len); out[len]='\0';
    return flipper_http_send_data(app->fhttp,out);
}

static void flip_rpi_send_special(FlipRPIApp *app, uint32_t key) {
    static const uint8_t ctrl_c[]={0x03}; static const uint8_t ctrl_x[]={0x18};
    static const uint8_t ctrl_z[]={0x1A}; static const uint8_t ctrl_d[]={0x04};
    static const uint8_t tab[]={0x09}; static const uint8_t esc[]={0x1B};
    static const uint8_t enter[]={0x0A}; static const uint8_t up[]={0x1B,'[','A'};
    static const uint8_t down[]={0x1B,'[','B'}; static const uint8_t right[]={0x1B,'[','C'};
    static const uint8_t left[]={0x1B,'[','D'};
    const uint8_t *d=NULL; size_t n=0;
    switch(key){
      case 0:d=ctrl_c;n=1;break; case 1:d=ctrl_x;n=1;break; case 2:d=ctrl_z;n=1;break;
      case 3:d=ctrl_d;n=1;break; case 4:d=tab;n=1;break; case 5:d=esc;n=1;break;
      case 6:d=enter;n=1;break; case 7:d=up;n=3;break; case 8:d=down;n=3;break;
      case 9:d=left;n=3;break; case 10:d=right;n=3;break;
    }
    if(d) flip_rpi_send_raw(app,d,n);
}

static int flip_rpi_alias(const char *x) {
    if(!strcmp(x,"!CTRL-C"))return 0; if(!strcmp(x,"!CTRL-X"))return 1;
    if(!strcmp(x,"!CTRL-Z"))return 2; if(!strcmp(x,"!CTRL-D"))return 3;
    if(!strcmp(x,"!TAB"))return 4; if(!strcmp(x,"!ESC"))return 5;
    if(!strcmp(x,"!ENTER"))return 6; if(!strcmp(x,"!UP"))return 7;
    if(!strcmp(x,"!DOWN"))return 8; if(!strcmp(x,"!LEFT"))return 9;
    if(!strcmp(x,"!RIGHT"))return 10; return -1;
}

static void flip_rpi_send_buffered_command(FlipRPIApp *app) {
    if(!app->ble_command_len) return;
    app->ble_command[app->ble_command_len]='\0';
    int alias=flip_rpi_alias(app->ble_command);
    if(alias>=0){ app->ble_command_len=0; flip_rpi_send_special(app,(uint32_t)alias); return; }
    char command[BLE_COMMAND_MAX+2]; size_t len=app->ble_command_len;
    memcpy(command,app->ble_command,len); command[len++]='\n'; command[len]='\0';
    app->ble_command_len=0; if(flip_rpi_ensure_uart(app)) flipper_http_send_data(app->fhttp,command);
}
static void flip_rpi_ble_flush_timer_callback(void *context){ FlipRPIApp *a=context; if(a&&a->view_dispatcher)view_dispatcher_send_custom_event(a->view_dispatcher,FlipRPICustomEventBLEFlush); }
static void flip_rpi_process_ble_rx(FlipRPIApp *app){
    FlipRPIBlePacket packet; bool any=false;
    while(furi_message_queue_get(app->ble_rx_queue,&packet,0)==FuriStatusOk){
      for(uint16_t i=0;i<packet.size;i++){ char c=(char)packet.data[i];
        if(c=='\r'||c=='\n'){ if(app->ble_send_timer)furi_timer_stop(app->ble_send_timer); flip_rpi_send_buffered_command(app); any=false; continue; }
        if(c=='\b'||(uint8_t)c==0x7F){ if(app->ble_command_len)app->ble_command_len--; any=app->ble_command_len>0; continue; }
        if(app->ble_command_len<BLE_COMMAND_MAX-1){ app->ble_command[app->ble_command_len++]=c; any=true; }
      }
    }
    if(any&&app->ble_send_timer){ furi_timer_stop(app->ble_send_timer); furi_timer_start(app->ble_send_timer,furi_ms_to_ticks(BLE_COMMAND_IDLE_MS)); }
}
static uint16_t flip_rpi_ble_serial_callback(SerialServiceEvent e,void *context){
    FlipRPIApp *a=context; if(!a||!a->ble_rx_queue)return 0;
    if(e.event==SerialServiceEventTypeDataReceived){ FlipRPIBlePacket p={0}; p.size=e.data.size>BLE_RX_PACKET_MAX?BLE_RX_PACKET_MAX:e.data.size; memcpy(p.data,e.data.buffer,p.size); if(furi_message_queue_put(a->ble_rx_queue,&p,0)==FuriStatusOk)view_dispatcher_send_custom_event(a->view_dispatcher,FlipRPICustomEventBLE); }
    return BLE_SERIAL_BUFFER_SIZE;
}
static bool flip_rpi_ble_start(FlipRPIApp *app){
    app->bt=furi_record_open(RECORD_BT); if(!app->bt)return false; bt_disconnect(app->bt); furi_delay_ms(200);
    bt_keys_storage_set_storage_path(app->bt,APP_DATA_PATH(".fliprpi_ble_v20.keys"));
    BleProfileSerialParams params={.device_name_prefix="FRPI20",.mac_xor=0x0200};
    app->ble_serial_profile=bt_profile_start(app->bt,ble_profile_serial,&params); if(!app->ble_serial_profile){bt_keys_storage_set_default_path(app->bt);furi_record_close(RECORD_BT);app->bt=NULL;return false;}
    ble_profile_serial_set_event_callback(app->ble_serial_profile,BLE_SERIAL_BUFFER_SIZE,flip_rpi_ble_serial_callback,app); furi_hal_bt_start_advertising(); return true;
}
static void flip_rpi_ble_stop(FlipRPIApp *app){
    if(!app->bt)return; if(app->ble_serial_profile)ble_profile_serial_set_event_callback(app->ble_serial_profile,0,NULL,NULL);
    bt_disconnect(app->bt); furi_delay_ms(200); bt_keys_storage_set_default_path(app->bt);
    if(app->ble_serial_profile){bt_profile_restore_default(app->bt);app->ble_serial_profile=NULL;} furi_record_close(RECORD_BT);app->bt=NULL;
}

static void update_special_submenu(FlipRPIApp *app){
    static const char *names[]={"Ctrl+C  Interrupt","Ctrl+X  Cut/Prefix","Ctrl+Z  Suspend","Ctrl+D  EOF/Logout","Tab  Complete","Esc","Enter","Up  History","Down  History","Left","Right"};
    for(uint32_t i=0;i<11;i++) submenu_add_item(app->submenu_special,names[i],SPECIAL_KEY_START+i,callback_submenu_choices,app);
}
static bool alloc_special_submenu(FlipRPIApp *app){
    if(app->submenu_special)return true;
    if(!easy_flipper_set_submenu(&app->submenu_special,FlipRPIViewSpecialKeys,"Terminal Keys",callback_to_submenu,&app->view_dispatcher))return false;
    update_special_submenu(app); return true;
}
static void free_special_submenu(FlipRPIApp *app){ if(app->submenu_special){view_dispatcher_remove_view(app->view_dispatcher,FlipRPIViewSpecialKeys);submenu_free(app->submenu_special);app->submenu_special=NULL;} }

'''
s=s.replace(marker,ble+marker,1)

one("    case FlipRPICustomEventUART:\n        flip_rpi_loader_process_callback(context);\n        return true;\n", "    case FlipRPICustomEventUART:\n        flip_rpi_loader_process_callback(context);\n        return true;\n    case FlipRPICustomEventBLE:\n        flip_rpi_process_ble_rx((FlipRPIApp*)context); return true;\n    case FlipRPICustomEventBLEFlush:\n        flip_rpi_send_buffered_command((FlipRPIApp*)context); return true;\n", "events handler")

one("    switch (index)\n    {\n    case FlipRPISubmenuIndexRun:", "    if(index>=SPECIAL_KEY_START && index<SPECIAL_KEY_START+11){ flip_rpi_send_special(app,index-SPECIAL_KEY_START); return; }\n    switch (index)\n    {\n    case FlipRPISubmenuIndexRun:", "special dispatch")
one("    case FlipRPISubmenuIndexSend:\n        free_submenu_command(app);", "    case FlipRPISubmenuIndexSpecialKeys:\n        free_special_submenu(app);\n        if(!alloc_special_submenu(app)) return;\n        view_dispatcher_switch_to_view(app->view_dispatcher,FlipRPIViewSpecialKeys);\n        break;\n    case FlipRPISubmenuIndexSend:\n        free_submenu_command(app);", "special case")

one("    FlipRPIApp *app = (FlipRPIApp *)malloc(sizeof(FlipRPIApp));\n\n    Gui *gui", "    FlipRPIApp *app = (FlipRPIApp *)malloc(sizeof(FlipRPIApp));\n    if(!app)return NULL; memset(app,0,sizeof(FlipRPIApp));\n\n    Gui *gui", "zero")
one('    submenu_add_item(app->submenu, "Send", FlipRPISubmenuIndexSend, callback_submenu_choices, app);\n    submenu_add_item(app->submenu, "About",', '    submenu_add_item(app->submenu, "Send", FlipRPISubmenuIndexSend, callback_submenu_choices, app);\n    submenu_add_item(app->submenu, "Terminal Keys", FlipRPISubmenuIndexSpecialKeys, callback_submenu_choices, app);\n    submenu_add_item(app->submenu, "About",', "main menu")
one('    submenu_add_item(app->submenu, "About", FlipRPISubmenuIndexAbout, callback_submenu_choices, app);\n    // submenu_add_item', '    submenu_add_item(app->submenu, "About", FlipRPISubmenuIndexAbout, callback_submenu_choices, app);\n\n    app->ble_rx_queue=furi_message_queue_alloc(8,sizeof(FlipRPIBlePacket));\n    app->ble_send_timer=furi_timer_alloc(flip_rpi_ble_flush_timer_callback,FuriTimerTypeOnce,app);\n    if(app->ble_rx_queue && app->ble_send_timer) flip_rpi_ble_start(app);\n    // submenu_add_item', "ble alloc")
one("    free_submenu_command(app);\n    free_text_box(app);\n\n    // free the FlipperHTTP", "    free_submenu_command(app);\n    free_special_submenu(app);\n    free_text_box(app);\n    flip_rpi_ble_stop(app);\n    if(app->ble_send_timer){furi_timer_stop(app->ble_send_timer);furi_timer_free(app->ble_send_timer);}\n    if(app->ble_rx_queue)furi_message_queue_free(app->ble_rx_queue);\n\n    // free the FlipperHTTP", "free")

p.write_text(s)
fam=Path("FlipRPI/application.fam"); fs=fam.read_text().replace("stack_size=4 * 1024","stack_size=8 * 1024").replace('fap_version="1.0"','fap_version="2.0"'); fam.write_text(fs)
print("FlipRPI v2: BLE input + terminal control keys")
