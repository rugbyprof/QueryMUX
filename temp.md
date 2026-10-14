# DB Survey Manifesto

I started this semester by designing a survey of db type backends (rdbms, document store, key value store, maybe more) and this evolved into the professor in me encouraging my students to write an api (python fast api) to run a set of queries on a fictitious db starting with sqlite. Seems like like an obvious starting point and allowed us to move forward and discover some of the downfalls (concurrency being one). 

The goal was to create similar api's on different db's (postgres, then mongo, and then we would see), and generate reports on performance using the query explain, timeing queries, and then looking at concurrency. I try to make the projects lean more toward a db user, and not a db administrator, while at the same time not ignoring ways to improve coding qaulity, speed, and safety. 

This lead me to get help writing a simple script called QueryMux, that allow a terminal based gui (or TUI). No frills, just a simple split screen with query on the left and result on the right with some syntax highlighting and code formatting. The goal is to have multiple db's as adaptive plugins to allow students to test and run queries from different technologies. Again, no frills, just basic querying as if we were logged into any one of a subset of db's terminal based access. 

After a few weeks of working and getting code generated from some AI's, I find myself focused on two repos: 

1. /Users/griffin/dev/QueryMUX
2. /Users/griffin/Library/Mobile Documents/com~apple~CloudDocs/_git_repos/5303-Adv-Database/ 

Where: 1) QueryMUX allows us to run queries against (eventually) any backend, and 2) 5303-Adv-Database is a repo that I'm creating to walk students through installation, configuration, and then testing  of a survey of db's. My problem is now that I have overwhelmed myself with erronious details that I need to forgo, and create similar paths of installation, configuration and usage for all the different db's that I want to teach. 

Problems are (for now) postgres and sqlite are very different beasts, especially when it comes to roles, db's, schemas, stored procedures, row level locks, table level locks etc.. 

I just know when I try to plugin mongodb as an adapter I will have to recreate most of the content to interact and query the document store. This is all ok, however, like I said, I've missed the forest for the trees and have approached sqlite, postgres, and soon mongo in surprisingly different fashions. Ultimately my goal is to have my QueryMux query runner work for an arbitrary number of db types, but to simplify and provide much commonality between each approach to each db's usage and testing. 

## Db Wishlist of Organization

In  `/Users/griffin/Library/Mobile Documents/com~apple~CloudDocs/_git_repos/5303-Adv-Database/` I have lots of generated lecture code and examples of things like API's. For now one with sqlite and also postgres. But for both of these I have: 
- setup instructions
- running queries
- walkthroughs
- performance 
- more code for demos 
- etc.

### What I would like: 

Is for you to (for now) run through `/Users/griffin/Library/Mobile Documents/com~apple~CloudDocs/_git_repos/5303-Adv-Database/` and provide me with a list / outline of a new more organized approach that incorporates QueryMux, and all provided api code for both sqlite and postgre (and postgis). I don't want fixes right now. Or more code generated. I need something that I can process at a slow pace that involves suggestions, outlines, topic suggestions, etc. so for the rest of my course and future courses will see an organized approach to:

1. A survey of rdbms's (1 or more), documents stores (1 or more), key value stores (1 or more) along with thier purposes, usage, and best practices.
2. QueryMux available to run simple queries agains any backend for simple testing purposes.
3. An approach to testing and analyzing queries to learn improvements and performance evalulation.
4. Load testing on one or more or all of the db's in our survey.
5. Lastly (should be firstly) what 5 db's (for lack of a better term) should we be testing?

Remember!! Help me organize and outline, then lets fufill babys steps of implementation / coding / tutorials with tons of explanations. 

