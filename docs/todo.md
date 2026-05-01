cache db/api responses

player typeahead

Update our players DB once a day (to make sure we have the right teams)

Add better typing to the response objects from api.py. Right now they just say 'dict'

Also list what the state of the game is when watching. Inning, top/bottom, outs.

Tests using fixtures + sample configs.

Test pulling people search results into fixtures, and then using those fixtures to test the people search command.

We need a way for users to add players they care about. We should just make a simple HTML website. 

Some kind of observability:
- disk usage
- ram usage
- cpu usage
- metrics: 
    - postgres usage:
        - number of connections
        - query latency
        - number of queries by type (e.g. insert into notification_log, select from users

    - our app:
        - How long does the API take to respond to requests? (latency) e.g. https://api.atbatwatch.prerak.net/me/follows
        - requests to each endpoint
        - number of users
        - number of follows
        - number of notifications sent
        - errors:
            - number of errors by type (e.g. mlb api error, db error, poller, fanout, delivery, etc.)
    - mlb api:
        - number of api calls made to each endpoint
- alerting:
    - if the app is down
    - if error rates go above a certain threshold
    - if mlb api calls go above a certain threshold (indicating we might be in a loop or something)

Questions:
- Github actions "CI". Do we have CI right now?
A: Yes
- What if we don't care about backup right now?
A: Already optional 
- bootstrap.sh creates a deploy user. How do I ssh/log-in as that user?
`ssh deploy@<your-hetzner-ip>` root keys are copied to deploy user (seems dangerous).
- Can we run shellcheck using uv?
No. brew locally. github CI can run shellcheck.
- .env file for db credentials? - seems dangerous to copy .env.example to .env in bootstrap.sh
github secrets for production. .env file for local development.
- Any other tokens or env vars or credentials we need to create? - Github secrets token
All the ones in deploy.yml must be added to github secretts. see docs/howto.md for how.


- We don't have a Github repo right now. 

- How can we see how much resources the app will take? storage, CPU, RAM, etc.? How many vCPUs we do we need? 



- How can we see that it still works locally before we deploy it now that we have three separate Python process (poller, fanout, and delivery)?

- Which instance should we actually use? 
  - CX23 2 VCPU, 4GB RAM $5/month, 20 TB traffic, GER hosted "cost optimized"
  - CX33, 4 VCPU, 8GB RAM, $8/month, 20 TB traffic,GER hosted "cost optimized"
  - CPX11 2 VCPU, 2GB RAM, $7/month, 1 TB traffic, USA hosted. "regular performance"



- Since we are not using a managed server, do we need to worry about server administration (security, backups, configuration, software, etc.)?



-------

> Does the DB persist at all between shutting down the Docker container? e.g. users, follows, or the test users I add (which I don't care about)

> run-all prints out a message for each poller check. Where does this get logged to? Do we need eventual log rotation

> Use pgcli. 

> How to deal with ambigous players to follow/unfollow/set notificactions. We should have the client pass in only integer/style player IDs. The client will call the MLB API itself to figure out what the players' IDs are. We can keep the by name lookup for now for local testing, though, since the CLI is essentially a client. 