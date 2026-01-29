import re

from agents import Runner

from dream.agent.prompt import build_fix_prompt, build_rewrite_prompt


async def evaluate_action(fix_action, rewrite_sql, query_info, *, db, fix_timeout, sql_timeout, fix_agent):
    fix_action, fix_sql = await derive_fix_action_and_sql(
        fix_action, rewrite_sql, query_info.query, db=db, fix_agent=fix_agent
    )

    print("Executing fix_action")

    if fix_action != "":
        success, _, _, err = db.execute(fix_action, timeout=fix_timeout)
    else:
        success = True
        err = ""
    print("Fix_action execution completed")

    retry_count = 0
    max_retry = 5
    while not success and retry_count < max_retry:
        prompt = build_fix_prompt(fix_action, err)
        result = await Runner.run(starting_agent=fix_agent, input=prompt)
        new_fix_action = result.final_output
        print(f"new_fix_action: {new_fix_action}")
        new_fix_action, new_fix_sql = await derive_fix_action_and_sql(
            new_fix_action, rewrite_sql, query_info.query, db=db, fix_agent=fix_agent
        )
        success, _, _, err = db.execute(new_fix_action, timeout=fix_timeout)
        fix_action = new_fix_action
        fix_sql = new_fix_sql
        retry_count += 1
    if not success:
        old_time = query_info.execution_time
        approve_time = 0.0
        return {
            "status": -1,
            "msg": f"SQL fix failed: {fix_action}, error: {err}",
            "fix_action": fix_action,
            "rewrite_sql": (fix_sql if fix_sql != query_info.query else ""),
            "new_time": sql_timeout,
            "old_time": old_time,
            "approve_time": approve_time,
        }

    old_time = query_info.execution_time

    print("Executing performance test")
    success, _, new_time, err = db.execute(fix_sql, timeout=sql_timeout)
    print("Performance test completed")

    # If we get here, ActionManager is responsible for rollback when needed.
    approve_time = old_time - new_time if success else 0.0

    if not success:
        if err and isinstance(err, str) and ("timeout" in err.lower() or "querycanceled" in err):
            return {
                "status": 0,
                "msg": f"Fix ineffective, performance not improved or degraded, original time {old_time:.4f}s, new time {new_time:.4f}s",
                "fix_action": fix_action,
                "rewrite_sql": (fix_sql if fix_sql != query_info.query else ""),
                "new_time": new_time,
                "old_time": old_time,
                "approve_time": approve_time,
            }
        return {
            "status": -1,
            "msg": f"Performance test SQL execution failed: {err}",
            "fix_action": fix_action,
            "rewrite_sql": (fix_sql if fix_sql != query_info.query else ""),
            "new_time": sql_timeout,
            "old_time": old_time,
            "approve_time": approve_time,
        }

    if (old_time - new_time) / old_time > 0.1:
        return {
            "status": 1,
            "msg": f"Fix successful, performance improved, time reduced from {old_time:.4f}s to {new_time:.4f}s",
            "fix_action": fix_action,
            "rewrite_sql": (fix_sql if fix_sql != query_info.query else ""),
            "new_time": new_time,
            "old_time": old_time,
            "approve_time": approve_time,
        }
    return {
        "status": 0,
        "msg": f"Fix ineffective, performance not improved or degraded, original time {old_time:.4f}s, new time {new_time:.4f}s",
        "fix_action": fix_action,
        "rewrite_sql": (fix_sql if fix_sql != query_info.query else ""),
        "new_time": new_time,
        "old_time": old_time,
        "approve_time": approve_time,
    }


async def derive_fix_action_and_sql(fix_action, rewrite_sql, origin_sql, *, db, fix_agent):
    match = re.search(r"/\*\+[\s\S]*?\*/", fix_action)
    if match:
        hint = match.group(0).strip()
        clean_fix_action = (fix_action[: match.start()] + fix_action[match.end() :]).strip()
        if rewrite_sql != "":
            rewrite_sql = await check_sql_rewrite(origin_sql, rewrite_sql, db=db, fix_agent=fix_agent)
            target_sql = rewrite_sql
        else:
            target_sql = origin_sql
        fix_sql = _inject_hint_into_sql(hint, target_sql)
        return clean_fix_action, fix_sql

    clean_fix_action = fix_action
    if rewrite_sql != "":
        rewrite_sql = await check_sql_rewrite(origin_sql, rewrite_sql, db=db, fix_agent=fix_agent)
        fix_sql = rewrite_sql
    else:
        fix_sql = origin_sql
    return clean_fix_action, fix_sql


def _inject_hint_into_sql(hint, sql):
    pattern = r"\b(WITH|SELECT|EXPLAIN)\b"

    def replace_match(match):
        start_pos = match.start()
        keyword = match.group(1).upper()

        left_count = sql[:start_pos].count("(")
        right_count = sql[:start_pos].count(")")

        if left_count > right_count:
            return match.group(0)

        if keyword == "SELECT":
            before_text = sql[:start_pos].lower().strip()
            if before_text.endswith("as"):
                return match.group(0)

        if keyword == "WITH":
            return match.group(0)

        if start_pos > 0:
            prefix = sql[:start_pos].strip()
            if prefix.endswith(hint.strip()):
                return match.group(0)

        return hint + " " + match.group(0)

    result = re.sub(pattern, replace_match, sql, flags=re.IGNORECASE)
    return result


async def check_sql_rewrite(origin_sql, rewrite_sql, *, db, fix_agent):
    print("start QED check")
    qed_match = db.check_sql_equivalence(origin_sql, rewrite_sql)
    print(f"QED equivalence check: {qed_match}")

    if qed_match:
        return rewrite_sql

    retry_count = 0
    max_retry = 5
    match = db.compare_sql_results(origin_sql, rewrite_sql)
    print(f"Result set comparison match: {match}")

    while not match and retry_count < max_retry:
        prompt = build_rewrite_prompt(origin_sql, rewrite_sql)
        result = await Runner.run(starting_agent=fix_agent, input=prompt)
        new_rewrite_sql = result.final_output
        print(f"new_rewrite_sql: {new_rewrite_sql}")
        rewrite_sql = new_rewrite_sql
        retry_count += 1
        match = db.compare_sql_results(origin_sql, rewrite_sql)
        print(f"Result set comparison match: {match}")

    if not match:
        print("Failed to rewrite SQL, return original SQL")
        return origin_sql

    return rewrite_sql
