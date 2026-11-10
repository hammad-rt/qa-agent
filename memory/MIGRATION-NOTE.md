# Memory migration (env-detail scrub)

Resource rows that stored URLs, logins, or record ids were retired and had `value_or_ref` cleared. Those values now come from pack.yaml, `.env`, or the current run’s answers (see `memory/run-prefs.json` for last-used account defaults only). Existing `learned.md` content was scrubbed in place where env details appeared; new writes are guarded in code.

Export scrub touched ~88 row(s) in memory-export.json.
