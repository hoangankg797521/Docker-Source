exports.up = (knex) => knex.schema.alterTable("monitor", t => t.text("additional_checks").nullable());
exports.down = (knex) => knex.schema.alterTable("monitor", t => t.dropColumn("additional_checks"));
