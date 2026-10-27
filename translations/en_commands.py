COMMANDS: dict[str, str] = {
    # ─── Help ────────────────────────────────────────────────────────
    "cmd_help_desc": "Shows all available commands",
    "cmd_info_desc": "Informations about Nexus",
    "cmd_diagnose_desc": "Checks whether Nexus has all required permissions",

    # ─── User Commands ───────────────────────────────────────────────
    "cmd_delete_desc": "Delete your Data on this Server.",
    "cmd_rank_desc":   "Shows your current Rank",
    "cmd_invite_desc": "Invite Nexus to your Server",
    "cmd_premium_desc": "Shows your server's Premium status",

    # ─── AutoMod Commands ────────────────────────────────────────────
    "cmd_automod_desc": "Configure AutoMod",

    # ─── Setup Commands ──────────────────────────────────────────────
    "cmd_setup_desc":   "Sets up Nexus",

    # ─── Mod Commands ────────────────────────────────────────────────
    "cmd_kick_desc":                    "Kicks a user",
    "cmd_kick_param_member":            "The user to kick",
    "cmd_kick_param_reason":            "The reason for the kick (optional)",
    "cmd_ban_desc":                     "Bans a user",
    "cmd_ban_param_member":             "The user to ban",
    "cmd_ban_param_reason":             "The reason for the ban (optional)",
    "cmd_ban_param_days":               "Delete message history from the past X days (0–7)",
    "cmd_unban_desc":                   "Unbans a user",
    "cmd_unban_param_member":           "The user to unban",
    "cmd_unban_param_reason":           "The reason for the unban (optional)",
    "cmd_add_role_desc":                "Assigns a role",
    "cmd_add_role_param_member":        "The member to receive the role",
    "cmd_add_role_param_role":          "The role to assign",
    "cmd_remove_role_desc":             "Removes a role",
    "cmd_remove_role_param_member":     "The member to remove the role from",
    "cmd_remove_role_param_role":       "The role to remove",
    "cmd_set_slowmode_desc":            "Sets the slowmode of a channel",
    "cmd_set_slowmode_param_channel":   "The channel to set slowmode in",
    "cmd_set_slowmode_param_duration":  "Choose the cooldown duration or 'Disabled'",
    "cmd_timeout_desc":                 "Times out a user",
    "cmd_timeout_param_member":         "The member to timeout",
    "cmd_timeout_param_duration":       "Duration of the timeout or 'Remove timeout' (0)",
    "cmd_timeout_param_reason":         "The reason for the timeout (optional)",
    "cmd_purge_desc":                   "Deletes multiple messages in a channel",
    "cmd_purge_param_channel":          "The channel where messages should be deleted",
    "cmd_purge_param_amount":           "Number of messages to delete",
    "cmd_warn_desc":                    "Warns a user",
    "cmd_warn_param_member":            "The user to warn",
    "cmd_warn_param_reason":            "The reason for the warning (optional)",
    "cmd_warnings_desc":                "Shows all warnings of a user",
    "cmd_warnings_param_member":        "The user whose warnings to display",
    "cmd_unwarn_desc":                  "Removes a single warning",
    "cmd_unwarn_param_member":          "The user whose warning should be removed",
    "cmd_unwarn_param_id":              "The ID of the warning to remove",
    "cmd_clearwarnings_desc":           "Clears all warnings of a user",
    "cmd_clearwarnings_param_member":   "The user whose warnings should be cleared",
    "cmd_timezone_desc":                "Set the timezone for scheduled events",
    "cmd_timezone_param_desc":          "Type your region or city (e.g., Berlin, New York, London...)",

    # ─── Admin Commands ──────────────────────────────────────────────
    "cmd_module_desc":          "Enable or disable modules",
    "cmd_settings_desc":        "Adjust server settings",

    # ─── Embed Commands ──────────────────────────────────────────────
    "cmd_embed":        "Create, post, schedule and manage embeds",
    "cmd_post_rules":   "Posts the server rules in the rules channel",

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
    "cmd_levelup_channel_desc": "Set the channel for level-up messages",
    "cmd_xp_channels_desc":     "Choose channels that earn no XP",

    # ─── MEE6 Importer Commands ─────────────────────────────────────────
    "cmd_import_mee6_desc": "Import XP from your public MEE6 leaderboard",
    "cmd_import_mee6_roles_desc": "Also import MEE6 level role rewards (default: yes)",

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
    "cmd_revoke_access_desc":                "Revokes this channel's support access",
    "cmd_support_module_config_desc":        "Shows the configuration of one of the server's modules",
    "cmd_support_module_config_param_desc":  "Which module should be shown?",
    "cmd_support_grant_premium_desc":        "Gifts the server temporary Premium",
    "cmd_support_grant_premium_days_desc":   "Duration in days (1–14)",
    "cmd_support_grant_premium_reason_desc": "Reason for granting (gets logged)",

    # ─── Giveaway Commands ──────────────────────────────────────────
    "cmd_giveaways_desc": "Open the giveaway overview to create or cancel giveaways",
}