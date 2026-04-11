from wizard.platforms import PLATFORMS, detect_platform
from wizard.paths import get_paths, load_env, save_env, set_env_var
from wizard.claude import MODEL, get_client, generate_candidates, generate_soul, compile_profile
from wizard.scraper import scrape_site, get_user_context
from wizard.tui import selection_ui
from wizard.editor import candidate_to_text, text_to_candidate, check_editor, edit_with_editor
from wizard.selection import run_selection_loop
from wizard.friends import generate_souls_for_selected, _validate_timezone, create_friend_dir, get_existing_friend_names
from wizard.telegram_setup import collect_bot_token
from wizard.checkpoint import load_checkpoint, save_checkpoint, clear_checkpoint
from wizard.steps import step_user_profile, step_history, step_select_friends, step_anthropic_key, step_telegram_bots, step_telegram_group, step_deploy, generate_history
from wizard.main import HOME_DIR, main
