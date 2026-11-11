"""Compare original live rows with a local backup using connection-local temporary tables.

Never restores or updates the live tables. New rows are allowed; old rows must match.
"""
import argparse
import json
import os
from pathlib import Path
from fresh_catalog_smoke import LOCAL, mysql_command


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backup', required=True)
    args = parser.parse_args()
    backup = Path(args.backup).read_text(encoding='utf-8-sig')
    state = json.loads((LOCAL / 'state.json').read_text(encoding='utf-8-sig'))
    mysql = Path(r'C:/Program Files/MySQL/MySQL Server 8.0/bin')
    env = dict(os.environ, MYSQL_PWD=state['rootPassword'])
    tables = ['users', 'stories', 'publications', 'readings', 'reading_steps', 'reviews', 'feedback']
    queries = ["SET NAMES utf8mb4; SET time_zone='+00:00';"]
    for table in tables:
        columns = mysql_command(mysql, state['dbPort'], 'root', 'juxia',
            f"SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='juxia' AND TABLE_NAME='{table}' ORDER BY ORDINAL_POSITION;", env).splitlines()
        assert columns and 'id' in columns, table
        temporary = 'qa_preserve_' + table
        queries.append(f'CREATE TEMPORARY TABLE `{temporary}` LIKE `{table}`;')
        for line in backup.splitlines():
            if line.startswith(f'INSERT INTO `{table}` '):
                queries.append(line.replace(f'INSERT INTO `{table}` ', f'INSERT INTO `{temporary}` ', 1))
        same = ' AND '.join(f'(old.`{column}` <=> live.`{column}`)' for column in columns)
        queries.append(f"SELECT '{table}',COUNT(*),COALESCE(SUM(live.id IS NULL OR NOT ({same})),0) FROM `{temporary}` old LEFT JOIN `{table}` live ON old.id=live.id;")
    result = mysql_command(mysql, state['dbPort'], 'root', 'juxia', '\n'.join(queries), env)
    checks = []
    for line in result.splitlines():
        table, count, changed = line.split('\t')
        checks.append({'table': table, 'originalRows': int(count), 'missingOrChanged': int(changed)})
        print(f'{table}: original={count}, missingOrChanged={changed}')
    passed = len(checks) == len(tables) and all(item['missingOrChanged'] == 0 for item in checks)
    (LOCAL / 'live-preservation.json').write_text(json.dumps({'passed': passed, 'checks': checks}, indent=2), encoding='utf-8')
    assert passed, 'Some original rows changed; live tables have not been modified by this check.'


if __name__ == '__main__':
    main()
