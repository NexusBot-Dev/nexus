COMMANDS: dict[str, str] = {
    # ─── Help ────────────────────────────────────────────────────────
    "cmd_help_desc": "Mostra todos os comandos disponíveis",
    "cmd_info_desc": "Informações sobre o Nexus",
    "cmd_diagnose_desc": "Verifica se o Nexus tem todas as permissões necessárias",

    # ─── User Commands ───────────────────────────────────────────────
    "cmd_delete_desc": "Exclui todos os seus dados neste servidor",
    "cmd_rank_desc":   "Mostra seu ranque atual",
    "cmd_invite_desc": "Convide o Nexus para o seu servidor",
    "cmd_premium_desc": "Mostra o status Premium do seu servidor",

    # ─── AutoMod Commands ────────────────────────────────────────────
    "cmd_automod_desc": "Configurar o AutoMod",

    # ─── Setup Commands ──────────────────────────────────────────────
    "cmd_setup_desc":   "Configura o Nexus",

    # ─── Mod Commands ────────────────────────────────────────────────
    "cmd_kick_desc":                    "Expulsa um usuário",
    "cmd_kick_param_member":            "O usuário que será expulso",
    "cmd_kick_param_reason":            "O motivo da expulsão (opcional)",
    "cmd_ban_desc":                     "Bane um usuário",
    "cmd_ban_param_member":             "O usuário que será banido",
    "cmd_ban_param_reason":             "O motivo do banimento (opcional)",
    "cmd_ban_param_days":               "Excluir histórico de mensagens dos últimos X dias (0–7)",
    "cmd_unban_desc":                   "Desbane um usuário",
    "cmd_unban_param_member":           "O usuário que será desbanido",
    "cmd_unban_param_reason":           "O motivo do desbanimento (opcional)",
    "cmd_add_role_desc":                "Atribui um cargo",
    "cmd_add_role_param_member":        "O membro que receberá o cargo",
    "cmd_add_role_param_role":          "O cargo a ser atribuído",
    "cmd_remove_role_desc":             "Remove um cargo",
    "cmd_remove_role_param_member":     "O membro do qual o cargo será removido",
    "cmd_remove_role_param_role":       "O cargo a ser removido",
    "cmd_set_slowmode_desc":            "Define o modo lento de um canal",
    "cmd_set_slowmode_param_channel":   "O canal onde o modo lento será aplicado",
    "cmd_set_slowmode_param_duration":  "Escolha a duração do tempo de espera ou 'Desativado'",
    "cmd_timeout_desc":                 "Aplica um castigo temporário a um usuário (timeout)",
    "cmd_timeout_param_member":         "O membro que receberá o castigo",
    "cmd_timeout_param_duration":       "Duração do castigo ou 'Remover castigo' (0)",
    "cmd_timeout_param_reason":         "O motivo do castigo (opcional)",
    "cmd_purge_desc":                   "Exclui várias mensagens em um canal",
    "cmd_purge_param_channel":          "O canal onde as mensagens serão excluídas",
    "cmd_purge_param_amount":           "Quantidade de mensagens a excluir",
    "cmd_warn_desc":                    "Adverte um usuário",
    "cmd_warn_param_member":            "O usuário que será advertido",
    "cmd_warn_param_reason":            "O motivo da advertência (opcional)",
    "cmd_warnings_desc":                "Mostra todas as advertências de um usuário",
    "cmd_warnings_param_member":        "O usuário cujas advertências serão exibidas",
    "cmd_unwarn_desc":                  "Remove uma advertência específica",
    "cmd_unwarn_param_member":          "O usuário do qual a advertência será removida",
    "cmd_unwarn_param_id":              "O ID da advertência a ser removida",
    "cmd_clearwarnings_desc":           "Limpa todas as advertências de um usuário",
    "cmd_clearwarnings_param_member":   "O usuário cujas advertências serão limpas",
    "cmd_timezone_desc":                "Define o fuso horário para eventos agendados",
    "cmd_timezone_param_desc":          "Digite sua região ou cidade (ex: Berlim, Nova York, Londres...)",

    # ─── Admin Commands ──────────────────────────────────────────────
    "cmd_module_desc":          "Ativar ou desativar módulos",
    "cmd_settings_desc":        "Ajustar as configurações do servidor",

    # ─── Embed Commands ──────────────────────────────────────────────
    "cmd_embed":        "Cria, envia, agenda e gerencia embeds",
    "cmd_post_rules":   "Envia as regras no canal de regras",

    # ─── Level Commands ──────────────────────────────────────────────
    "cmd_leaderboard_desc":     "Exibe o Top 10 membros",
    "cmd_set_level_desc":       "Define o nível de um usuário",
    "cmd_add_xp_desc":          "Adiciona EXP a um usuário",
    "cmd_remove_xp_desc":       "Remove EXP de um usuário",
    "cmd_wipe_xp_desc":         "Redefine o EXP e nível de um usuário",
    "cmd_silent_desc":          "Se ativado, a resposta só será visível para você",
    "cmd_member_desc":          "Selecione um membro para ver o ranque",
    "cmd_level_desc":           "O nível a ser definido",
    "cmd_amount_desc":          "A quantidade de EXP a adicionar",
    "cmd_remove_amount_desc":   "A quantidade de EXP a remover",
    "cmd_level_roles_desc":     "Gerenciar cargos por nível",
    "cmd_levelup_channel_desc": "Define o canal para as mensagens de subida de nível",

    # ─── MEE6 Importer Commands ─────────────────────────────────────────
    "cmd_import_mee6_desc": "Importe o XP do placar de líderes público do MEE6",
    "cmd_import_mee6_roles_desc": "Também importar cargos de recompensa por nível do MEE6 (padrão: sim)",

    # ─── React Commands ──────────────────────────────────────────────
    "cmd_rr_create_desc":       "Cria uma mensagem de cargos por reação",
    "cmd_rr_list_desc":         "Mostra todas as mensagens de cargos por reação",
    "cmd_rr_delete_desc":       "Exclui uma mensagem de cargos por reação",

    # ─── Stream Alert Commands ───────────────────────────────────────
    "cmd_alert_add_desc":       "Adiciona um canal para monitorar",
    "cmd_alert_remove_desc":    "Remove um canal do monitoramento",
    "cmd_alert_list_desc":      "Exibe todos os canais monitorados",
    "cmd_alert_role_desc":      "Define um cargo para marcar em alertas de transmissão",
    "cmd_alert_role_role_desc": "Defina o cargo — deixe vazio para remover",
    "cmd_alert_role_platform_desc": "Apenas Premium: definir para uma plataforma específica",
    "cmd_alert_platform_desc":  "Selecione a plataforma (ex: Twitch, YouTube)",
    "cmd_alert_channel_desc":   "Em qual canal os alertas devem ser enviados?",
    "cmd_alert_edit_desc":      "Altera o canal ou a mensagem de uma live monitorada",

    # ─── Announcement Commands ───────────────────────────────────────
    "cmd_announcement_desc":         "Ativar/desativar anúncios e definir canal",
    "cmd_announcement_enabled_desc": "Ativar ou desativar anúncios",
    "cmd_announcement_channel_desc": "Canal para futuros anúncios",

    # ─── Welcome Commands ────────────────────────────────────────────
    "cmd_welcome_message_desc": "Gerenciar mensagens de boas-vindas",
    "cmd_goodbye_desc":         "Ativar/desativar despedidas e definir canal",

    # ─── Ticket Commands ──────────────────────────────────────────────
    "cmd_add_category_desc":        "Cria uma nova categoria de tickets",
    "cmd_edit_category_desc":       "Edita uma categoria de tickets existente",
    "cmd_edit_category_param_desc": "Qual categoria deve ser editada?",
    "cmd_ticket_panel_desc":        "Envia o painel de tickets com botões",
    "cmd_ticket_panel_channel_desc":"Em qual canal enviar o painel?",
    "cmd_force_close_desc":         "Fecha um ticket permanentemente (motivo obrigatório)",

    # ─── Boost Commands ─────────────────────────────────────────────
    "cmd_boost_message_desc":   "Gerenciar mensagens de boost",

    # ─── Support Commands ───────────────────────────────────────────
    "cmd_support_pin_desc":         "Gera um PIN de suporte",
    "cmd_verify_pin_desc":          "Verifica um PIN para acessar os comandos de suporte",
    "cmd_verify_pin_param_desc":    "Digite o PIN que você recebeu",
    "cmd_support_guild_info_desc":  "Exibe informações do servidor ao qual você tem acesso de suporte",
    "cmd_revoke_access_desc":                "Revoga o acesso de suporte deste canal",
    "cmd_support_module_config_desc":        "Exibe a configuração de um módulo do servidor",
    "cmd_support_module_config_param_desc":  "Qual módulo você quer ver?",
    "cmd_support_grant_premium_desc":        "Concede Premium temporário ao servidor",
    "cmd_support_grant_premium_days_desc":   "Duração em dias (1–365)",
    "cmd_support_grant_premium_reason_desc": "Motivo da concessão (fica registrado)",

    # ─── Giveaway Commands ──────────────────────────────────────────
    "cmd_giveaways_desc": "Abra o painel de sorteios para criar ou cancelar",
}