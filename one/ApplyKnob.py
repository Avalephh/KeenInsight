import time
import sys
import os
import configparser
from database.knobs import logger
from database.knobs import initialize_knobs

class ApplyKnob:
    """
    A class to apply a set of recommended parameters (knobs) to a database.
    It handles parameter validation, online/offline application, and error recovery.
    Supports both MySQL (via MysqlDB) and PostgreSQL (via PGConnector).
    """
    def __init__(self, db, knobs_detail, online_mode=False, reinit=False):
        """
        Initialize the ApplyKnob class.

        Args:
            db: An instance of the database class (e.g., MysqlDB).
            knobs_detail (dict): Detailed information about each knob (min, max, type, etc.).
            online_mode (bool): Whether to apply knobs online (True) or offline (False).
            reinit (bool): Whether to re-initialize the database if applying knobs fails.
        """
        self.db = db
        self.knobs_detail = knobs_detail
        self.online_mode = online_mode
        self.reinit = reinit

    @classmethod
    def from_pg_connector(cls, online_mode=True, reinit=False, **pg_kwargs):
        """Create an ApplyKnob instance backed by a live PostgreSQL connection.

        pg_kwargs are forwarded to PGConnector (host, port, user, password,
        dbname, conf_path).  Omit any to use the PGConnector defaults (which
        read from PG_HOST / PG_PORT / … env vars or fall back to 127.0.0.1:5432).
        """
        from pg_connector import PGConnector

        pg = PGConnector(**pg_kwargs)
        pg.connect()

        # Build a knobs_detail dict from pg_settings so _validate_knobs works.
        raw = pg.collect_knobs()
        knobs_detail = {}
        for name, meta in raw.items():
            vtype = meta.get("vartype", "")
            knobs_detail[name] = {
                "type": "integer" if vtype == "integer" else vtype,
                "min": meta.get("min_val"),
                "max": meta.get("max_val"),
            }

        return cls(pg, knobs_detail, online_mode=online_mode, reinit=reinit)

    @classmethod
    def from_config(cls, config_path, knob_num=-1, online_mode=None, reinit=False):
        from database.mysqldb import MysqlDB

        config = configparser.ConfigParser(
            defaults={"here": os.path.dirname(os.path.abspath(config_path))}
        )
        read_files = config.read(config_path)
        if not read_files:
            raise FileNotFoundError(config_path)

        if 'database' not in config:
            raise ValueError("Missing [database] section in config")

        db_section = config['database']
        db_args = {
            'db': db_section.get('db', 'mysql').strip(),
            'host': db_section.get('host', '127.0.0.1').strip(),
            'port': int(db_section.get('port', '3306')),
            'user': db_section.get('user', 'root').strip(),
            'passwd': db_section.get('passwd', '').strip(),
            'sock': db_section.get('sock', '').strip(),
            'cnf': db_section.get('cnf', '').strip(),
            'mysqld': db_section.get('mysqld', '').strip(),
            'remote_mode': db_section.get('remote_mode', 'False').strip(),
            'isolation_mode': db_section.get('isolation_mode', 'False').strip(),
            'knob_config_file': db_section.get('knob_config_file', '').strip(),
            'knob_num': int(db_section.get('knob_num', str(knob_num))),
            'dbname': db_section.get('dbname', '').strip(),
            'pid': int(db_section.get('pid', '0')),
        }

        if online_mode is None:
            online_mode = eval(db_section.get('online_mode', 'False'))

        if not db_args['knob_config_file']:
            raise ValueError("knob_config_file is empty in config")

        db = MysqlDB(db_args)
        knobs_detail = initialize_knobs(db_args['knob_config_file'], int(db_args['knob_num']))
        return cls(db, knobs_detail, online_mode=online_mode, reinit=reinit)

    def _validate_knobs(self, knobs):
        """
        Validate and adjust knobs based on their allowed ranges (min/max).
        
        Args:
            knobs (dict): The set of knobs to validate.
        """
        for key in list(knobs.keys()):
            meta = self.knobs_detail.get(key)
            if not isinstance(meta, dict):
                continue

            if meta.get('type') != 'integer':
                continue

            try:
                value = int(knobs[key])
            except Exception:
                continue

            min_v = meta.get('min')
            max_v = meta.get('max')
            try:
                if min_v is not None:
                    min_v = int(min_v)
                if max_v is not None:
                    max_v = int(max_v)
            except Exception:
                min_v = None
                max_v = None

            if min_v is not None and value < min_v:
                knobs[key] = min_v
                continue

            if max_v is not None and value > max_v:
                knobs[key] = max_v
                continue

            knobs[key] = value
        
        return knobs

    def apply(self, recommended_knobs):
        """
        Apply the recommended knobs to the database.

        Args:
            recommended_knobs (dict): The set of parameters to apply.

        Returns:
            bool: True if the application was successful, False otherwise.
        """
        knobs = self._validate_knobs(recommended_knobs.copy())

        logger.info(f"Applying recommended knobs: {knobs}")

        try:
            if self.online_mode:
                flag = self.db.apply_knobs_online(knobs)
            else:
                flag = self.db.apply_knobs_offline(knobs)
            
            if not flag:
                logger.error("Apply knobs failed!")
                if self.reinit:
                    logger.info("Re-initializing database...")
                    self.db.reinitdb_magic()
                    logger.info("Database re-initialized.")
                return False

            logger.info("Knobs applied successfully!")
            return True

        except Exception as e:
            logger.error(f"Exception occurred during knob application: {e}")
            if self.reinit:
                logger.info("Re-initializing database due to exception...")
                try:
                    self.db.reinitdb_magic()
                except Exception as reinit_e:
                    logger.error(f"Re-initialization failed: {reinit_e}")
            return False
        

if __name__ == "__main__":
    # This section is for basic structure demonstration. 
    # Real usage requires a properly initialized 'db' object and 'knobs_detail'.
    print("ApplyKnob class loaded.")

    # 调用方式
    config_path = os.path.join(os.path.dirname(__file__), 'database', 'config_template.ini')
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), 'database', 'config_template.ini')

    applier = ApplyKnob.from_config(config_path=config_path, knob_num=-1, online_mode=False, reinit=False)

    recommended_parameters = {
        'innodb_buffer_pool_size': 1024 * 1024 * 1024,
        'innodb_log_file_size': 256 * 1024 * 1024,
        'max_connections': 500,
        'innodb_flush_log_at_trx_commit': 1
    }

    print("Applying recommended parameters...")
    success = applier.apply(recommended_parameters)

    if success:
        print("Parameters applied successfully and database is ready!")
    else:
        print("Failed to apply parameters.")