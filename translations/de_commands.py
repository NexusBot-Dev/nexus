COMMANDS: dict[str, str] = {
    # ─── Help ────────────────────────────────────────────────────────
    "cmd_help_desc": "Zeigt alle verfügbaren Commands",
    "cmd_info_desc": "Informationen über Nexus",
    "cmd_diagnose_desc": "Prüft, ob Nexus alle nötigen Rechte hat",

    # ─── User Commands ───────────────────────────────────────────────
    "cmd_delete_desc": "Löscht alle deine Daten auf diesem Server",
    "cmd_rank_desc":   "Zeigt deinen aktuellen Rang",
    "cmd_invite_desc": "Lad Nexus auf deinen Server ein",
    "cmd_premium_desc": "Zeigt den Premium-Status deines Servers",

    # ─── AutoMod Commands ────────────────────────────────────────────
    "cmd_automod_desc": "AutoMod konfigurieren",

    # ─── Setup Commands ──────────────────────────────────────────────
    "cmd_setup_desc":   "Nexus einrichten",

    # ─── Mod Commands ────────────────────────────────────────────────
    "cmd_kick_desc":            "Kickt einen User",
    "cmd_kick_param_member":    "Der User, der gekickt werden soll",
    "cmd_kick_param_reason":    "Der Grund für den Kick (optional)",
    "cmd_ban_desc":             "Bannt einen User",
    "cmd_ban_param_member":     "Der User, der gebannt werden soll",
    "cmd_ban_param_reason":     "Der Grund für den Ban(optional)",
    "cmd_ban_param_days":       "Löscht Nachrichten der letzten X Tage (0–7)",
    "cmd_unban_desc":           "Entbannt einen User",
    "cmd_unban_param_member":   "Der User, der entbannt werden soll",
    "cmd_unban_param_reason":   "Der Grund für den Unban (optional)",
    "cmd_add_role_desc":        "Vergibt eine Rolle",
    "cmd_add_role_param_member":    "Das Mitglied, das die Rolle erhalten soll",
    "cmd_add_role_param_role":  "Die zu vergebende Rolle",
    "cmd_remove_role_desc":     "Entziehe eine Rolle",
    "cmd_remove_role_param_member": "Das Mitglied, das die Rolle entzogen bekommen soll",
    "cmd_remove_role_param_role":   "Die zu entziehende Rolle",
    "cmd_set_slowmode_desc":    "Setzt den Slowmode eines Channels",
    "cmd_set_slowmode_param_channel":   "Der Channel, der in den Slowmode gesetzt werden soll",
    "cmd_set_slowmode_param_duration":  "Wähle die gewünschte Abklingzeit oder 'Deaktiviert'",
    "cmd_timeout_desc":         "Gibt einem User einen Timeout",
    "cmd_timeout_param_member":     "Das Mitglied, das getimeoutet werden soll",
    "cmd_timeout_param_duration":   "Dauer des Timeouts oder 'Aufheben' (0)",
    "cmd_timeout_param_reason":     "Der Grund für den Timeout (optional)",
    "cmd_purge_desc":           "Löscht mehrere Nachrichten in einem Channel",
    "cmd_purge_param_channel":  "Der Channel, in dem die Nachrichten gelöscht werden sollen",
    "cmd_purge_param_amount":   "Anzahl der zu löschenden Nachrichten",
    "cmd_warn_desc":            "Verwarnt einen User",
    "cmd_warn_param_member":    "Der User, der verwarnt werden soll",
    "cmd_warn_param_reason":    "Der Grund für die Verwarnung (optional)",
    "cmd_warnings_desc":        "Zeigt alle Verwarnungen eines Users",
    "cmd_warnings_param_member":    "Der User, dessen Warnungen angezeigt werden sollen",
    "cmd_unwarn_desc":          "Entfernt eine einzelne Verwarnung",
    "cmd_unwarn_param_member":  "Der User, dessen Warnung gelöscht werden soll",
    "cmd_unwarn_param_id":      "Die ID der zu löschenden Verwarnung",
    "cmd_clearwarnings_desc":   "Löscht alle Verwarnungen eines Users",
    "cmd_clearwarnings_param_member":   "Der User, dessen Warnungen alle gelöscht werden sollen",
    "cmd_timezone_desc":        "Setze die Zeitzone für zeitgesteuerte Events",
    "cmd_timezone_param_desc":  "Tippe deine Region oder Stadt (z.B. Berlin, New York, London...)",

    # ─── Admin Commands ──────────────────────────────────────────────
    "cmd_module_desc":          "Module aktivieren oder deaktivieren",
    "cmd_settings_desc":        "Server-Einstellungen anpassen",

    # ─── Embed Commands ──────────────────────────────────────────────
    "cmd_embed":        "Embeds erstellen, posten, planen und verwalten",
    "cmd_post_rules":   "Postet die Serverregeln in den Regelchannel",

    # ─── Level Commands ──────────────────────────────────────────────
    "cmd_leaderboard_desc":     "Zeigt die Top 10 Member",
    "cmd_set_level_desc":       "Setzt das Level eines Users",
    "cmd_add_xp_desc":          "Fügt einem User EXP hinzu",
    "cmd_remove_xp_desc":       "Entfernt EXP von einem User",
    "cmd_wipe_xp_desc":         "Setzt EXP und Level eines Users zurück",
    "cmd_silent_desc":          "Wenn aktiviert, wird die Antwort nur für dich sichtbar sein",
    "cmd_member_desc":          "Wähle ein Mitglied aus, um dessen Rang zu sehen",
    "cmd_level_desc":           "Das Level, das gesetzt werden soll",
    "cmd_amount_desc":          "Die Menge an EXP, die hinzugefügt werden soll",
    "cmd_remove_amount_desc":   "Die Menge an EXP, die entfernt werden soll",
    "cmd_level_roles_desc":     "Level-Rollen verwalten",

    # ─── React Commands ──────────────────────────────────────────────
    "cmd_rr_create_desc":       "Erstellt eine Reaktionsrollen-Nachricht",
    "cmd_rr_list_desc":         "Zeigt alle Reaktionsrollen-Nachrichten",
    "cmd_rr_delete_desc":       "Löscht eine Reaktionsrollen-Nachricht",

    # ─── Stream Alert Commands ───────────────────────────────────────
    "cmd_alert_add_desc":     "Kanal zum Überwachen hinzufügen",
    "cmd_alert_remove_desc":  "Kanal aus der Überwachung entfernen",
    "cmd_alert_list_desc":    "Zeigt alle überwachten Kanäle",
    "cmd_alert_role_desc":    "Legt eine Rolle fest, die bei Stream-Alerts gepingt wird",
    "cmd_alert_role_role_desc": "Rolle festlegen — leer lassen, um sie zu entfernen",
    "cmd_alert_role_platform_desc": "Nur mit Premium: für eine bestimmte Plattform statt für alle setzen",
    "cmd_alert_platform_desc":      "Plattform auswählen (z.B. Twitch, YouTube)",
    "cmd_alert_role_platform_desc": "Nur mit Premium: für eine bestimmte Plattform statt für alle setzen",
    "cmd_alert_channel_desc":            "In welchem Kanal sollen die Alerts gepostet werden?",
    "cmd_alert_edit_desc": "Ändert Channel oder Nachricht eines überwachten Kanals",

    # ─── Announcement Commands ───────────────────────────────────────
    "cmd_announcement_desc":         "Announcements aktivieren/deaktivieren und Channel festlegen",
    "cmd_announcement_enabled_desc": "Announcements aktivieren oder deaktivieren",
    "cmd_announcement_channel_desc": "Channel für zukünftige Announcements festlegen",

    # ─── Welcome Commands ────────────────────────────────────────────
    "cmd_welcome_message_desc": "Willkommensnachrichten verwalten",
    "cmd_goodbye_desc":         "Goodbyes aktivieren/deaktivieren und Channel festlegen",

    # ─── Ticket Commands ─────────────────────────────────────────────
    "cmd_add_category_desc":        "Erstellt eine neue Ticket-Kategorie",
    "cmd_edit_category_desc":       "Bearbeitet eine bestehende Ticket-Kategorie",
    "cmd_edit_category_param_desc": "Welche Kategorie soll bearbeitet werden?",
    "cmd_ticket_panel_desc":        "Postet das Ticket-Panel mit Kategorie-Buttons",
    "cmd_ticket_panel_channel_desc":"In welchem Channel soll das Panel gepostet werden?",
    "cmd_force_close_desc":         "Schließt ein Ticket permanent (mit Begründung)",

    # ─── Boost Commands ─────────────────────────────────────────────
    "cmd_boost_message_desc": "Manage boost messages",

    # ─── Support Commands ───────────────────────────────────────────
    "cmd_support_pin_desc":  "Erstellt einen Support-PIN",
    "cmd_verify_pin_desc": "Verifiziert einen Support-PIN, um Zugriff auf die Support-Commands zu erhalten",
    "cmd_verify_pin_param_desc": "Gib den PIN ein, den du erhalten hast",
    "cmd_support_guild_info_desc": "Zeigt Informationen über den Server, auf den du Support-Zugriff hast",
    "cmd_revoke_access_desc":                "Widerruft den Support-Zugriff dieses Channels",
    "cmd_support_module_config_desc":        "Zeigt die Konfiguration eines Moduls des Servers",
    "cmd_support_module_config_param_desc":  "Welches Modul soll angezeigt werden?",
    "cmd_support_grant_premium_desc":        "Schenkt dem Server befristet Premium",
    "cmd_support_grant_premium_days_desc":   "Dauer in Tagen (1–365)",
    "cmd_support_grant_premium_reason_desc": "Grund für die Vergabe (wird protokolliert)",

    # ─── Giveaway Commands ──────────────────────────────────────────
    "cmd_giveaways_desc": "Öffne die Giveaway-Übersicht zum Erstellen oder Abbrechen",
}