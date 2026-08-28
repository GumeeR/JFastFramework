"""Code more than one module needs.

The rule this directory exists to make obvious: **modules do not import each
other.** Two modules that reach into each other are one module with a folder
between them -- neither can be extracted into a service later, and a change to
one breaks the other in a way no test covers.

So when a second module needs the same enum, type or pure function, it moves
here. `jfast contracts check` reports the cross-import and names the file to
move it to, so this is a rule with a check behind it rather than a paragraph
somebody read once.

The direction is one-way and enforced: **modules import shared/, shared/ never
imports a module.** Without that, this becomes the place everything ends up and
the dependency graph is a circle.

What belongs here:

* enums whose values are part of more than one module's vocabulary
* value objects and types shared across modules
* pure functions -- no database, no HTTP

What does not:

* anything that touches the database. Two modules sharing a repository is two
  modules sharing a table, which is how a set of microservices becomes a
  distributed monolith with extra latency.
* anything only one module uses. It goes in that module, and moves here on the
  day a second one wants it.
"""
