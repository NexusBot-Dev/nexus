COMMAND_LOCALES: dict[str, dict[str, str]] = {
    "de": {
        # ─── Help ────────────────────────────────────────────────────────
        "cmd_help_desc": "Zeigt alle verfügbaren Commands",
        "cmd_info_desc": "Informationen über Nexus",

        # ─── User Commands ───────────────────────────────────────────────
        "cmd_delete_desc": "Löscht alle deine Daten auf diesem Server",
        "cmd_rank_desc":   "Zeigt deinen aktuellen Rang",
        "cmd_invite_desc": "Lad Nexus auf deinen Server ein",

        # ─── AutoMod Commands ────────────────────────────────────────────
        "cmd_automod_desc": "AutoMod konfigurieren",

        # ─── Setup Commands ──────────────────────────────────────────────
        "cmd_setup_desc":   "Nexus einrichten",

        # ─── Mod Commands ────────────────────────────────────────────────
        "cmd_kick_desc":            "Kickt einen User",
        "cmd_ban_desc":             "Bannt einen User",
        "cmd_unban_desc":           "Entbannt einen User",
        "cmd_add_role_desc":        "Vergibt eine Rolle",
        "cmd_remove_role_desc":     "Entziehe eine Rolle",
        "cmd_set_slowmode_desc":    "Setzt den Slowmode eines Channels",
        "cmd_timeout_desc":         "Gibt einem User einen Timeout",
        "cmd_purge_desc":           "Löscht mehrere Nachrichten in einem Channel",
        "cmd_warn_desc":            "Verwarnt einen User",
        "cmd_warnings_desc":        "Zeigt alle Verwarnungen eines Users",
        "cmd_unwarn_desc":          "Entfernt eine einzelne Verwarnung",
        "cmd_clearwarnings_desc":   "Löscht alle Verwarnungen eines Users",
        "cmd_timezone_desc":        "Setze die Zeitzone für zeitgesteuerte Events",
        "cmd_timezone_param_desc":  "Tippe deine Region oder Stadt (z.B. Berlin, New York, London...)",

        # ─── Admin Commands ──────────────────────────────────────────────
        "cmd_module_desc":          "Module aktivieren oder deaktivieren",
        "cmd_settings_desc":        "Server-Einstellungen anpassen",

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
    },

    "en": {
        # ─── Help ────────────────────────────────────────────────────────
        "cmd_help_desc": "Shows all available commands",
        "cmd_info_desc": "Informations about Nexus",

        # ─── User Commands ───────────────────────────────────────────────
        "cmd_delete_desc": "Delete your Data on this Server.",
        "cmd_rank_desc":   "Shows your current Rank",
        "cmd_invite_desc": "Invite Nexus to your Server",

        # ─── AutoMod Commands ────────────────────────────────────────────
        "cmd_automod_desc": "Configure AutoMod",

        # ─── Setup Commands ──────────────────────────────────────────────
        "cmd_setup_desc":   "Sets up Nexus",

        # ─── Mod Commands ────────────────────────────────────────────────
        "cmd_kick_desc":            "Kicks a user from the server",
        "cmd_ban_desc":             "Bans a user from the server",
        "cmd_unban_desc":           "Unbans a user from the server",
        "cmd_add_role_desc":        "Assigns a role to a user",
        "cmd_remove_role_desc":     "Removes a role from a user",
        "cmd_set_slowmode_desc":    "Sets the slowmode duration for a channel",
        "cmd_timeout_desc":         "Times out a user for a specific duration",
        "cmd_purge_desc":           "Deletes a large number of messages in a channel",
        "cmd_warn_desc":            "Issues a warning to a user",
        "cmd_warnings_desc":        "Displays all warnings of a user",
        "cmd_unwarn_desc":          "Removes a single warning from a user",
        "cmd_clearwarnings_desc":   "Clears all warnings of a user",
        "cmd_timezone_desc":        "Sets the timezone for time-based events",
        "cmd_timezone_param_desc":  "Type your region or city (e.g., Berlin, New York, London...)",

        # ─── Admin Commands ──────────────────────────────────────────────
        "cmd_module_desc":          "Enable or disable modules",
        "cmd_settings_desc":        "Adjust server settings",

        # ─── Level Commands ──────────────────────────────────────────────
        "cmd_leaderboard_desc":     "Displays the top 10 members",
        "cmd_set_level_desc":       "Sets the level of a user",
        "cmd_add_xp_desc":          "Adds XP to a user",
        "cmd_remove_xp_desc":       "Removes XP from a user",
        "cmd_wipe_xp_desc":         "Resets a user's XP and level",
        "cmd_silent_desc":          "If enabled, the response will only be visible to you",
        "cmd_member_desc":          "Select a member to view their rank",
        "cmd_level_desc":           "The level to set",
        "cmd_amount_desc":          "The amount of XP to add",
        "cmd_remove_amount_desc":   "The amount of XP to remove",
        "cmd_level_roles_desc":     "Manage level roles",

        # ─── React Commands ──────────────────────────────────────────────
        "cmd_rr_create_desc":       "Creates a reaction role message",
        "cmd_rr_list_desc":         "Displays all reaction role messages",
        "cmd_rr_delete_desc":       "Deletes a reaction role message",

        # ─── Stream Alert Commands ────────────────────────────────────────
        "cmd_alert_add_desc":     "Adds a channel to be monitored",
        "cmd_alert_remove_desc":  "Removes a channel from monitoring",
        "cmd_alert_list_desc":    "Displays all monitored channels",
        "cmd_alert_role_desc":    "Sets a role to be pinged for stream alerts",
        "cmd_alert_role_role_desc": "Set a role — leave empty to remove it",
        "cmd_alert_role_platform_desc": "Premium only: Set for a specific platform instead of all",
        "cmd_alert_platform_desc":      "Select the platform (e.g. Twitch, YouTube)",
        "cmd_alert_role_platform_desc": "Premium only: Set for a specific platform instead of all",
        "cmd_alert_channel_desc":            "Which channel should the alerts be posted in?",
        "cmd_alert_edit_desc": "Changes the channel or message of a monitored channel",

        # ─── Announcement Commands ───────────────────────────────────────
        "cmd_announcement_desc":         "Enable/disable announcements and set a channel",
        "cmd_announcement_enabled_desc": "Enable or disable announcements",
        "cmd_announcement_channel_desc": "Set the channel for future announcements",

        # ─── Welcome Commands ─────────────────────────────────────────────
        "cmd_welcome_message_desc": "Manage welcome messages",
        "cmd_goodbye_desc":         "Enable/disable Goodbyes and set a channel",

        # ─── Ticket Commands ──────────────────────────────────────────────
        "cmd_add_category_desc":        "Creates a new ticket category",
        "cmd_edit_category_desc":       "Edits an existing ticket category",
        "cmd_edit_category_param_desc": "Which category should be edited?",
        "cmd_ticket_panel_desc":        "Posts the ticket panel with category buttons",
        "cmd_ticket_panel_channel_desc":"Which channel should the panel be posted in?",
        "cmd_force_close_desc":         "Force-closes a ticket (reason required)",

        # ─── Boost Commands ─────────────────────────────────────────────
        "cmd_boost_message_desc": "Manage boost messages",

        # ─── Support Commands ───────────────────────────────────────────
        "cmd_support_pin_desc": "Creates a support PIN",
        "cmd_verify_pin_desc": "Verifies a support PIN to gain access to the support Commands",
        "cmd_verify_pin_param_desc": "Enter the PIN you received",
        "cmd_support_guild_info_desc": "Shows information about the server you have support access to",
    }
}