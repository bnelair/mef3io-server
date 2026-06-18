import logging
import os
from datetime import datetime

def setup_logging(log_dir, log_level=logging.INFO):
    """Setup logging with both file and console handlers.
    
    Args:
        log_dir (str): Directory to store log files.
        log_level (int): Logging level (e.g., logging.INFO, logging.DEBUG).
    
    Returns:
        str: Path to the log file.
    """
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime('%Y-%m-%dT%H-%M-%S')
    log_file = os.path.join(log_dir, f'server_{timestamp}.log')
    log_format = '%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s'
    
    root_logger = logging.getLogger()
    # Remove all handlers first (for repeated tests/reloads)
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)
    
    # File handler
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(file_handler)
    
    # Console handler (for stdout)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(console_handler)
    
    root_logger.setLevel(log_level)
    
    root_logger.info(f"Logging initialized. Log file: {log_file}, Level: {logging.getLevelName(log_level)}")
    
    return log_file

def get_logger(name: str):
    """Get a logger instance for the given name.
    
    Args:
        name (str): Name of the logger (typically module name).
    
    Returns:
        logging.Logger: Logger instance.
    """
    return logging.getLogger(name)
