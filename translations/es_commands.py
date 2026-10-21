COMMANDS: dict[str, str] = {
    # ─── Help ────────────────────────────────────────────────────────
    "cmd_help_desc": "Muestra todos los comandos disponibles",
    "cmd_info_desc": "Información sobre Nexus",
    "cmd_diagnose_desc": "Comprueba si Nexus tiene todos los permisos necesarios",

    # ─── User Commands ───────────────────────────────────────────────
    "cmd_delete_desc": "Elimina todos tus datos en este servidor",
    "cmd_rank_desc":   "Muestra tu rango actual",
    "cmd_invite_desc": "Invita a Nexus a tu servidor",
    "cmd_premium_desc": "Muestra el estado Premium de tu servidor",

    # ─── AutoMod Commands ────────────────────────────────────────────
    "cmd_automod_desc": "Configurar AutoMod",

    # ─── Setup Commands ──────────────────────────────────────────────
    "cmd_setup_desc":   "Configura Nexus",

    # ─── Mod Commands ────────────────────────────────────────────────
    "cmd_kick_desc":                    "Expulsa a un usuario",
    "cmd_kick_param_member":            "El usuario que se va a expulsar",
    "cmd_kick_param_reason":            "El motivo de la expulsión (opcional)",
    "cmd_ban_desc":                     "Banea a un usuario",
    "cmd_ban_param_member":             "El usuario que se va a banear",
    "cmd_ban_param_reason":             "El motivo del baneo (opcional)",
    "cmd_ban_param_days":               "Eliminar historial de mensajes de los últimos X días (0–7)",
    "cmd_unban_desc":                   "Desbanea a un usuario",
    "cmd_unban_param_member":           "El usuario que se va a desbanear",
    "cmd_unban_param_reason":           "El motivo del desbaneo (opcional)",
    "cmd_add_role_desc":                "Asigna un rol",
    "cmd_add_role_param_member":        "El miembro que recibirá el rol",
    "cmd_add_role_param_role":          "El rol a asignar",
    "cmd_remove_role_desc":             "Quita un rol",
    "cmd_remove_role_param_member":     "El miembro al que se le quitará el rol",
    "cmd_remove_role_param_role":       "El rol a quitar",
    "cmd_set_slowmode_desc":            "Ajusta el modo lento de un canal",
    "cmd_set_slowmode_param_channel":   "El canal donde se aplicará el modo lento",
    "cmd_set_slowmode_param_duration":  "Elige la duración del tiempo de espera o 'Desactivado'",
    "cmd_timeout_desc":                 "Aísla temporalmente a un usuario (timeout)",
    "cmd_timeout_param_member":         "El miembro que se va a aislar",
    "cmd_timeout_param_duration":       "Duración del aislamiento o 'Quitar aislamiento' (0)",
    "cmd_timeout_param_reason":         "El motivo del aislamiento (opcional)",
    "cmd_purge_desc":                   "Elimina varios mensajes en un canal",
    "cmd_purge_param_channel":          "El canal donde se eliminarán los mensajes",
    "cmd_purge_param_amount":           "Cantidad de mensajes a eliminar",
    "cmd_warn_desc":                    "Advierte a un usuario",
    "cmd_warn_param_member":            "El usuario que se va a advertir",
    "cmd_warn_param_reason":            "El motivo de la advertencia (opcional)",
    "cmd_warnings_desc":                "Muestra todas las advertencias de un usuario",
    "cmd_warnings_param_member":        "El usuario cuyas advertencias se mostrarán",
    "cmd_unwarn_desc":                  "Elimina una advertencia específica",
    "cmd_unwarn_param_member":          "El usuario al que se le eliminará la advertencia",
    "cmd_unwarn_param_id":              "La ID de la advertencia que se va a eliminar",
    "cmd_clearwarnings_desc":           "Elimina todas las advertencias de un usuario",
    "cmd_clearwarnings_param_member":   "El usuario cuyas advertencias se eliminarán",
    "cmd_timezone_desc":                "Establece la zona horaria para eventos programados",
    "cmd_timezone_param_desc":          "Escribe tu región o ciudad (ej. Berlín, Nueva York, Londres...)",

    # ─── Admin Commands ──────────────────────────────────────────────
    "cmd_module_desc":          "Activar o desactivar módulos",
    "cmd_settings_desc":        "Ajustar la configuración del servidor",

    # ─── Embed Commands ──────────────────────────────────────────────
    "cmd_embed":        "Crea, publica, programa y gestiona embeds",
    "cmd_post_rules":   "Publica las reglas en el canal de normas",

    # ─── Level Commands ──────────────────────────────────────────────
    "cmd_leaderboard_desc":     "Muestra el Top 10 de miembros",
    "cmd_set_level_desc":       "Establece el nivel de un usuario",
    "cmd_add_xp_desc":          "Añade EXP a un usuario",
    "cmd_remove_xp_desc":       "Quita EXP a un usuario",
    "cmd_wipe_xp_desc":         "Restablece la EXP y el nivel de un usuario",
    "cmd_silent_desc":          "Si se activa, la respuesta solo será visible para ti",
    "cmd_member_desc":          "Selecciona un miembro para ver su rango",
    "cmd_level_desc":           "El nivel a establecer",
    "cmd_amount_desc":          "Cantidad de EXP a añadir",
    "cmd_remove_amount_desc":   "Cantidad de EXP a quitar",
    "cmd_level_roles_desc":     "Gestionar roles por nivel",
    "cmd_levelup_channel_desc": "Establece el canal para los mensajes de subida de nivel",

    # ─── MEE6 Importer Commands ─────────────────────────────────────────
    "cmd_import_mee6_desc": "Importa la EXP de la tabla de clasificación pública de MEE6",
    "cmd_import_mee6_roles_desc": "Importa también las recompensas de roles por nivel de MEE6 (por defecto: sí)",

    # ─── React Commands ──────────────────────────────────────────────
    "cmd_rr_create_desc":       "Crea un mensaje de roles por reacción",
    "cmd_rr_list_desc":         "Muestra todos los mensajes de roles por reacción",
    "cmd_rr_delete_desc":       "Elimina un mensaje de roles por reacción",

    # ─── Stream Alert Commands ───────────────────────────────────────
    "cmd_alert_add_desc":       "Añade un canal para monitorizar",
    "cmd_alert_remove_desc":    "Elimina un canal de la monitorización",
    "cmd_alert_list_desc":      "Muestra todos los canales monitorizados",
    "cmd_alert_role_desc":      "Establece un rol para mencionar en alertas de stream",
    "cmd_alert_role_role_desc": "Elige un rol — déjalo vacío para eliminarlo",
    "cmd_alert_role_platform_desc": "Solo Premium: asignar para una plataforma específica",
    "cmd_alert_platform_desc":  "Selecciona la plataforma (ej. Twitch, YouTube)",
    "cmd_alert_channel_desc":   "¿En qué canal se deben publicar las alertas?",
    "cmd_alert_edit_desc":      "Modifica el canal o mensaje de un directo monitorizado",

    # ─── Announcement Commands ───────────────────────────────────────
    "cmd_announcement_desc":         "Activar/desactivar anuncios y fijar un canal",
    "cmd_announcement_enabled_desc": "Activar o desactivar anuncios",
    "cmd_announcement_channel_desc": "Canal para futuros anuncios",

    # ─── Welcome Commands ────────────────────────────────────────────
    "cmd_welcome_message_desc": "Gestionar mensajes de bienvenida",
    "cmd_goodbye_desc":         "Activar/desactivar despedidas y fijar un canal",

    # ─── Ticket Commands ──────────────────────────────────────────────
    "cmd_add_category_desc":        "Crea una nueva categoría de tickets",
    "cmd_edit_category_desc":       "Edita una categoría de tickets existente",
    "cmd_edit_category_param_desc": "¿Qué categoría deseas editar?",
    "cmd_ticket_panel_desc":        "Publica el panel de tickets con botones",
    "cmd_ticket_panel_channel_desc":"¿En qué canal publicar el panel?",
    "cmd_force_close_desc":         "Cierra un ticket permanentemente (requiere motivo)",

    # ─── Boost Commands ─────────────────────────────────────────────
    "cmd_boost_message_desc":   "Gestionar mensajes de boost",

    # ─── Support Commands ───────────────────────────────────────────
    "cmd_support_pin_desc":         "Genera un PIN de soporte",
    "cmd_verify_pin_desc":          "Verifica un PIN para acceder a los comandos de soporte",
    "cmd_verify_pin_param_desc":    "Introduce el PIN que recibiste",
    "cmd_support_guild_info_desc":  "Muestra información sobre el servidor al que tienes acceso de soporte",
    "cmd_revoke_access_desc":                "Revoca el acceso de soporte de este canal",
    "cmd_support_module_config_desc":        "Muestra la configuración de un módulo del servidor",
    "cmd_support_module_config_param_desc":  "¿Qué módulo quieres ver?",
    "cmd_support_grant_premium_desc":        "Regala Premium temporal al servidor",
    "cmd_support_grant_premium_days_desc":   "Duración en días (1–365)",
    "cmd_support_grant_premium_reason_desc": "Motivo de la concesión (se registra)",

    # ─── Giveaway Commands ──────────────────────────────────────────
    "cmd_giveaways_desc": "Abre el panel de sorteos para crear o cancelar",
}