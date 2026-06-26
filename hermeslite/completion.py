"""Shell completion scripts for bash, zsh, and fish."""
from __future__ import annotations

COMMANDS = [
    "chat", "web", "serve", "config", "models", "tools", "skills",
    "sessions", "memory", "cron", "logs", "dump", "profile", "redact",
    "setup", "init", "install", "uninstall", "reset", "where", "doctor",
    "version", "completion", "status",
]

CONFIG_SUBCMDS = ["show", "path", "get", "set", "wizard"]
SESSIONS_SUBCMDS = ["list", "show", "delete"]
MEMORY_SUBCMDS = ["list", "show", "add", "del", "search"]
CRON_SUBCMDS = ["list", "add", "remove", "enable", "disable", "run-once", "start", "stop"]
PROFILE_SUBCMDS = ["list", "path", "use"]


def bash_completion() -> str:
    """Generate bash completion script."""
    return '''_hermeslite() {
    local cur prev commands
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    commands="''' + ' '.join(COMMANDS) + '''"

    if [[ ${cur} == -* ]]; then
        COMPREPLY=( $(compgen -W "--help --config --profile --debug --no-color --log-level" -- ${cur}) )
        return 0
    fi

    if [[ ${COMP_CWORD} -eq 1 ]]; then
        COMPREPLY=( $(compgen -W "${commands}" -- ${cur}) )
        return 0
    fi

    local subcmd="${COMP_WORDS[1]}"
    case "${subcmd}" in
        config)
            COMPREPLY=( $(compgen -W "''' + ' '.join(CONFIG_SUBCMDS) + '''" -- ${cur}) ) ;;
        sessions)
            COMPREPLY=( $(compgen -W "''' + ' '.join(SESSIONS_SUBCMDS) + '''" -- ${cur}) ) ;;
        memory)
            COMPREPLY=( $(compgen -W "''' + ' '.join(MEMORY_SUBCMDS) + '''" -- ${cur}) ) ;;
        cron)
            COMPREPLY=( $(compgen -W "''' + ' '.join(CRON_SUBCMDS) + '''" -- ${cur}) ) ;;
        profile)
            COMPREPLY=( $(compgen -W "''' + ' '.join(PROFILE_SUBCMDS) + '''" -- ${cur}) ) ;;
    esac
    return 0
}
complete -F _hermeslite hermeslite
'''


def zsh_completion() -> str:
    """Generate zsh completion script."""
    cmds = ' '.join(f'"{c}"' for c in COMMANDS)
    return f'''#compdef hermeslite

_hermeslite() {{
    _arguments \\
        '1:command:({cmds})' \\
        '*::arg:->args'
}}

_hermeslite "$@"
'''


def fish_completion() -> str:
    """Generate fish completion script."""
    cmds = ' '.join(f'"{c}"' for c in COMMANDS)
    return f'''complete -c hermeslite -f
complete -c hermeslite -n '__fish_use_subcommand' -a '{cmds}'
complete -c hermeslite -n '__fish_seen_subcommand_from config' -a 'show path get set wizard'
complete -c hermeslite -n '__fish_seen_subcommand_from sessions' -a 'list show delete'
complete -c hermeslite -n '__fish_seen_subcommand_from memory' -a 'list show add del search'
complete -c hermeslite -n '__fish_seen_subcommand_from cron' -a 'list add remove enable disable run-once start stop'
complete -c hermeslite -n '__fish_seen_subcommand_from profile' -a 'list path use'
'''


def get_completion(shell: str) -> str:
    """Get the completion script for the given shell."""
    if shell == "bash":
        return bash_completion()
    if shell == "zsh":
        return zsh_completion()
    if shell == "fish":
        return fish_completion()
    raise ValueError(f"Unsupported shell: {shell}")
